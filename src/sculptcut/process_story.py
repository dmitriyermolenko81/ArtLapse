"""Chronological accelerated process, preserving the work between milestones."""

import math
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import cv2
import numpy as np
from numpy.typing import NDArray

from sculptcut.budget import distribute
from sculptcut.development import (
    DevelopmentEvidence,
    DevelopmentScorer,
    chronological_weights,
    development_weight,
)
from sculptcut.domain import Project
from sculptcut.planning import RenderDecision, RenderPlan, validate_plan
from sculptcut.screen_cleanup import project_screens
from sculptcut.segmentation import Segment


@dataclass(frozen=True)
class StorySettings:
    chapters: int = 45
    chapter_spacing_power: float = 1.0
    early_chapters: int = 0
    pause_us: int = 6_000_000
    navigation_us: int = 1_200_000
    navigation_confidence: float = 0.6
    continuity_weight: float = 0.5
    navigation_penalty: float = 0.5
    chunk_us: int = 300_000_000
    early_weight: float = 1.0
    final_seconds: int = 4
    max_speed: int = 8
    episode_us: int = 6_000_000
    candidate_step_us: int = 2_000_000
    screen_drop_us: int = 1_000_000
    visual_candidates: int = 12


STORY = StorySettings()


def work_chapters(
    decisions: list[RenderDecision],
    power: float = STORY.chapter_spacing_power,
    count: int = STORY.chapters,
) -> tuple[list[RenderDecision], list[int]]:
    """Spread milestones over retained work, without spending chapters on pauses."""
    total = sum(
        d.source_end_us - d.source_start_us
        for d in decisions
        if d.provenance == "user:process_story"
    )
    boundaries = sorted(
        {round(total * (i / count) ** power) for i in range(count + 1)}
    )
    result: list[RenderDecision] = []
    chapters: list[int] = []
    position = 0
    for decision in decisions:
        if decision.provenance != "user:process_story":
            result.append(decision)
            chapters.append(-1)
            continue
        length = decision.source_end_us - decision.source_start_us
        cuts = [position] + [b for b in boundaries if position < b < position + length]
        cuts.append(position + length)
        for a, b in zip(cuts, cuts[1:], strict=False):
            result.append(
                decision.model_copy(
                    update={
                        "source_start_us": decision.source_start_us + a - position,
                        "source_end_us": decision.source_start_us + b - position,
                    }
                )
            )
            chapters.append(min(count - 1, bisect_right(boundaries, a) - 1))
        position += length
    return result, chapters


def readable_episodes(
    decisions: list[RenderDecision],
    chapters: list[int],
    segments: list[Segment],
    visual_score: Callable[[str, int, int], float] | None = None,
    visual_signature: Callable[[str, int], NDArray[np.uint8] | None] | None = None,
    development: DevelopmentScorer | None = None,
    chapter_count: int = STORY.chapters,
    episode_lengths_us: list[int] | None = None,
) -> list[RenderDecision]:
    """Keep one continuous work episode per temporal chapter, without micro-cuts."""
    chosen: dict[int, tuple[int, int]] = {}
    prior_views: list[NDArray[np.uint8]] = []
    prior_states: dict[str, DevelopmentEvidence] = {}
    for chapter in range(chapter_count):
        best: tuple[float, int, int, int] | None = None
        best_view: NDArray[np.uint8] | None = None
        best_evidence = DevelopmentEvidence()
        candidates: list[tuple[float, int, int, int]] = []
        for index, decision in enumerate(decisions):
            if chapters[index] != chapter or decision.provenance != "user:process_story":
                continue
            a, b = decision.source_start_us, decision.source_end_us
            length = min(episode_lengths_us[chapter] if episode_lengths_us else STORY.episode_us,
                         b - a)
            relevant = [
                s
                for s in segments
                if s.source_id == decision.source_id and s.start_us < b and s.end_us > a
            ]
            starts = sorted(set(range(a, b - length + 1, STORY.candidate_step_us)) | {b - length})
            for start in starts:
                end = start + length
                # Unknown work remains eligible, but sustained classified work wins.
                score = sum(
                    max(0, min(end, s.end_us) - max(start, s.start_us))
                    * (1.0 if s.kind == "SCULPT" else 0.25 if s.kind in ("MIXED", "UNKNOWN") else 0)
                    for s in relevant
                )
                longest_work = max(
                    (
                        max(0, min(end, s.end_us) - max(start, s.start_us))
                        for s in relevant
                        if s.kind == "SCULPT"
                    ),
                    default=0,
                )
                navigation = sum(
                    max(0, min(end, s.end_us) - max(start, s.start_us))
                    for s in relevant
                    if s.kind == "CAMERA" and s.score >= STORY.navigation_confidence
                )
                score += longest_work * STORY.continuity_weight
                score -= navigation * STORY.navigation_penalty
                candidate = (score + length * 0.05, -index, -start, end)
                candidates.append(candidate)
        if visual_score is not None and candidates:
            # Check both high-scoring work and alternatives across the chapter;
            # equal activity scores must not always choose the first seconds.
            ranked = sorted(candidates, reverse=True)[: STORY.visual_candidates]
            step = max(1, len(candidates) // STORY.visual_candidates)
            shortlist = sorted(set(ranked + candidates[::step]), reverse=True)
            for score, index, start, end in shortlist:
                quality = visual_score(decisions[-index].source_id, -start, end)
                if quality <= 0:
                    continue
                view = (
                    visual_signature(decisions[-index].source_id, end - 200_000)
                    if visual_signature is not None
                    else None
                )
                if view is not None and len(prior_views) >= 2:
                    older = [
                        float(np.mean(cv2.absdiff(view, past).astype(np.float32)))
                        for past in prior_views[-8:-1]
                    ]
                    latest = float(np.mean(cv2.absdiff(view, prior_views[-1]).astype(np.float32)))
                    if older and min(older) < 3 and latest > 10:
                        # An A→B→A-looking return is ambiguous evidence of Undo.
                        # Prefer another verified moment; never delete the source.
                        quality *= 0.15
                source_id = decisions[-index].source_id
                evidence = (
                    development(source_id, -start, end)
                    if development is not None else DevelopmentEvidence()
                )
                quality *= development_weight(evidence, prior_states.get(source_id))
                candidate = (score * quality, index, start, end)
                if best is None or candidate > best:
                    best = candidate
                    best_view = view
                    best_evidence = evidence
        elif candidates:
            best = max(candidates)
        if best is not None:
            chosen[-best[1]] = (-best[2], best[3])
            prior_states[decisions[-best[1]].source_id] = best_evidence
            if best_view is not None:
                prior_views.append(best_view)
    result: list[RenderDecision] = []
    for index, decision in enumerate(decisions):
        if decision.provenance != "user:process_story":
            result.append(decision)
            continue
        window = chosen.get(index)
        boundaries = [decision.source_start_us, decision.source_end_us]
        if window:
            boundaries = sorted(set(boundaries + list(window)))
        for a, b in zip(boundaries, boundaries[1:], strict=False):
            keep = window is not None and a >= window[0] and b <= window[1]
            result.append(
                decision.model_copy(
                    update={
                        "source_start_us": a,
                        "source_end_us": b,
                        "action": "SPEED" if keep else "DROP",
                        "output_frame_count": 1 if keep else 0,
                        "provenance": "user:process_story" if keep else "user:process_omission",
                    }
                )
            )
    return result


def process_plan(
    root: Path,
    project: Project,
    base: RenderPlan,
    segments: list[Segment],
    cancel: Event | None = None,
) -> RenderPlan:
    from sculptcut.sculpt_only import final_start

    fps = base.output_fps
    music_grid: list[int] = []
    if project.settings.preset == "music_shorts":
        from sculptcut.music_episodes import music_episode_grid

        music_grid = music_episode_grid(root, project, base.target_frame_count, cancel)
    screens = project_screens(root, project, cancel)
    last = project.sources[-1]
    final_length = min(max(
        STORY.final_seconds * 1_000_000,
        math.ceil((music_grid[-1] - music_grid[-2]) * 1_000_000 / fps) if music_grid else 0,
    ), last.duration_us)
    final = final_start(root, project, last, final_length, cancel)
    final_end = min(last.duration_us, final + final_length) if final is not None else 0
    decisions: list[RenderDecision] = []
    for source in project.sources:
        rejected = [(r.start_us, r.end_us) for r in screens if r.source_id == source.id]
        rejected += [
            (s.start_us, s.end_us)
            for s in segments
            if s.source_id == source.id
            and (
                s.kind == "STATIC"
                and s.end_us - s.start_us >= STORY.pause_us
                or s.kind in ("INTERFACE", "PROCESSING")
                or project.settings.sculpt_only
                and s.kind == "CAMERA"
                and s.score >= STORY.navigation_confidence
                and s.end_us - s.start_us >= STORY.navigation_us
            )
        ]
        rejected += [
            (o.start_us, o.end_us)
            for o in project.overrides
            if o.source_id == source.id and o.action == "DROP"
        ]
        merged: list[tuple[int, int]] = []
        for a, b in sorted(rejected):
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
            else:
                merged.append((a, b))
        cuts = {0, source.duration_us}
        for a, b in merged:
            cuts.update((a, b))
        if source.id == last.id and final is not None:
            cuts.update((final, final_end))
        ordered = sorted(cuts)
        for a, b in zip(ordered, ordered[1:], strict=False):
            is_final = source.id == last.id and final is not None and final <= a < final_end
            drop = any(left < b and right > a for left, right in merged)
            # A stable final view may be STATIC, but explicit user drops/screens win.
            if is_final:
                drop = any(
                    r.source_id == source.id and r.start_us < b and r.end_us > a for r in screens
                )
                drop |= any(
                    o.source_id == source.id
                    and o.action == "DROP"
                    and o.start_us < b
                    and o.end_us > a
                    for o in project.overrides
                )
            if source.id == last.id and final is not None and a >= final_end:
                drop = True
            chunks = (
                1 if drop or is_final else max(1, (b - a + STORY.chunk_us - 1) // STORY.chunk_us)
            )
            for part in range(chunks):
                left, right = a + (b - a) * part // chunks, a + (b - a) * (part + 1) // chunks
                decisions.append(
                    RenderDecision(
                        source_id=source.id,
                        source_start_us=left,
                        source_end_us=right,
                        action="DROP" if drop else "SPEED",
                        output_frame_count=0 if drop else 1,
                        provenance="user:process_omission"
                        if drop
                        else "user:shorts_final"
                        if is_final
                        else "user:process_story",
                    )
                )
    power = (
        project.settings.shorts_early_chapter_power
        if sum(s.duration_us for s in project.sources)
        > project.settings.shorts_early_focus_min_seconds * 1_000_000
        else 1.0
    )
    if music_grid:
        # Beat slots need readable source windows even in the opening chapters;
        # the quadratic early focus can make those shorter than a single slot.
        power = 1.0
    chapter_count = STORY.chapters
    episode_lengths: list[int] | None = None
    if project.settings.preset == "music_shorts":
        from sculptcut.music_episodes import SETTINGS

        work_us = sum(d.source_end_us - d.source_start_us for d in decisions
                      if d.provenance == "user:process_story")
        if music_grid:
            slots = music_grid[:-1] if any(
                d.action != "DROP" and d.provenance == "user:shorts_final" for d in decisions
            ) else music_grid
            chapter_count = max(1, min(
                len(slots) - 1, work_us * fps // 1_000_000,
            ))
            boundaries = [slots[i * (len(slots) - 1) // chapter_count]
                          for i in range(chapter_count + 1)]
            episode_lengths = [max(
                math.ceil((b - a) * 1_000_000 / fps),
                min(STORY.episode_us, (b - a) * STORY.max_speed * 1_000_000 // fps),
            ) for a, b in zip(boundaries, boundaries[1:], strict=False)]
        else:
            chapter_count = max(1, min(
                STORY.chapters, work_us // STORY.episode_us,
                round(base.target_frame_count / fps / SETTINGS.preferred_shot_seconds),
            ))
    decisions, chapters = work_chapters(decisions, power, chapter_count)
    chapter_lookup = {
        (d.source_id, d.source_start_us): ch for d, ch in zip(decisions, chapters, strict=True)
    }
    original_decisions = decisions
    from sculptcut.shorts_quality import EpisodeInspector

    with EpisodeInspector(root, project, cancel) as inspector:
        decisions = readable_episodes(
            decisions, chapters, segments, inspector.score, inspector.signature,
            inspector.development,
            chapter_count,
            episode_lengths,
        )
        development_weights = chronological_weights(
            ((d.source_id, d.source_start_us, d.source_end_us) for d in decisions
             if d.provenance == "user:process_story"), inspector.development,
        )
    # Selection splits each original decision into omission/episode/omission.
    chapters = []
    index = 0
    for decision in decisions:
        while index + 1 < len(original_decisions) and (
            original_decisions[index].source_id != decision.source_id
            or decision.source_start_us >= original_decisions[index].source_end_us
        ):
            index += 1
        original = original_decisions[index]
        chapters.append(chapter_lookup[(original.source_id, original.source_start_us)])
    caps = [
        0
        if d.action == "DROP"
        else max(1, (d.source_end_us - d.source_start_us) * fps // 1_000_000)
        for d in decisions
    ]
    episode_weights = [
        development_weights.get((d.source_id, d.source_start_us, d.source_end_us), 1.0)
        for d in decisions
    ]
    final_counts = [
        c if d.provenance == "user:shorts_final" else 0
        for c, d in zip(caps, decisions, strict=True)
    ]
    if not any(caps):
        raise ValueError("Plan contains no retained footage")
    target = min(base.target_frame_count, sum(caps))
    work_budget = target - sum(final_counts)
    totals = [
        sum(
            c
            for c, d, ch in zip(caps, decisions, chapters, strict=True)
            if ch == i and d.provenance == "user:process_story"
        )
        for i in range(chapter_count)
    ]
    chapter_counts = distribute(
        [
            sum(
                max(1, (c + STORY.max_speed - 1) // STORY.max_speed)
                for c, d, ch in zip(caps, decisions, chapters, strict=True)
                if ch == i and d.provenance == "user:process_story"
            )
            for i in range(chapter_count)
        ],
        [
            (STORY.early_weight if chapter < STORY.early_chapters else 1.0)
            * sum(
                cap * weight
                for cap, weight, ch in zip(caps, episode_weights, chapters, strict=True)
                if ch == chapter
            ) / max(1, totals[chapter])
            if totals[chapter] else 1.0
            for chapter in range(chapter_count)
        ],
        totals,
        work_budget,
    )
    counts = final_counts[:]
    for chapter, budget in enumerate(chapter_counts):
        indices = [
            i
            for i, d in enumerate(decisions)
            if chapters[i] == chapter and d.provenance == "user:process_story"
        ]
        if not indices:
            continue
        allocation = distribute(
            [max(1, (caps[i] + STORY.max_speed - 1) // STORY.max_speed) for i in indices],
            [float(caps[i]) * episode_weights[i] for i in indices],
            [caps[i] for i in indices],
            budget,
        )
        for i, n in zip(indices, allocation, strict=True):
            counts[i] = n
    result = base.model_copy(
        update={
            "decisions": [
                d.model_copy(update={"output_frame_count": n})
                for d, n in zip(decisions, counts, strict=True)
            ],
            "target_frame_count": base.target_frame_count,
            "actual_frame_count": sum(counts),
            "settings": base.settings
            | {
                "preset": project.settings.preset,
                "story": "process",
                "bookends": True,
                "final_view_frames": sum(final_counts),
                "width": 1080,
                "height": 1920,
            },
            "warnings": ["Chronological continuous episodes; process speed limited to 8x"],
        }
    )
    validate_plan(project, result)
    return result
