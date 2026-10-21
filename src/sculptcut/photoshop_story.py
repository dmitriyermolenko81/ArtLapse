"""Chronological, readable Photoshop highlights from a long working session."""

from collections.abc import Callable
from dataclasses import dataclass

from sculptcut.budget import distribute
from sculptcut.domain import Project, SourceMedia
from sculptcut.planning import RenderDecision, RenderPlan, validate_plan


@dataclass(frozen=True)
class PhotoshopStorySettings:
    episodes: int = 45
    episode_us: int = 12_000_000
    max_speed: int = 16
    target_episode_seconds: float = 1.2
    candidates: int = 3
    final_us: int = 4_000_000


STORY = PhotoshopStorySettings()


def episodic_photoshop_plan(
    project: Project,
    base: RenderPlan,
    score_window: Callable[[SourceMedia, int, int], float] | None = None,
    progress: Callable[[float], None] | None = None,
) -> RenderPlan:
    """Sample sustained work across the entire edit instead of speeding up everything."""
    last = project.sources[-1]
    final_start = max(0, last.duration_us - STORY.final_us)
    prepared: list[RenderDecision] = []
    for decision in base.decisions:
        cuts = [decision.source_start_us]
        if (
            decision.source_id == last.id
            and decision.source_start_us < final_start < decision.source_end_us
        ):
            cuts.append(final_start)
        cuts.append(decision.source_end_us)
        for start, end in zip(cuts, cuts[1:], strict=False):
            final = (decision.source_id == last.id and start >= final_start
                     and decision.provenance != "user:DROP")
            prepared.append(
                decision.model_copy(
                    update={
                        "source_start_us": start,
                        "source_end_us": end,
                        "action": "SPEED" if final else decision.action,
                        "output_frame_count": 1 if final else decision.output_frame_count,
                        "provenance": "user:final_tail" if final else decision.provenance,
                    }
                )
            )
    work = [d for d in prepared if d.action != "DROP" and d.provenance != "user:final_tail"]
    total = sum(d.source_end_us - d.source_start_us for d in work)
    final_frames = sum(
        (d.source_end_us - d.source_start_us) * base.output_fps // 1_000_000
        for d in prepared
        if d.provenance == "user:final_tail"
    )
    episode_frames = max(1, round(base.output_fps * STORY.target_episode_seconds))
    chapters = min(
        STORY.episodes,
        max(1, (base.target_frame_count - final_frames) // episode_frames),
    )
    if total <= chapters * STORY.episode_us:
        return _allocate(project, base, prepared)

    # Place milestones in retained-work time, so a long pause cannot consume one.
    windows: dict[int, list[tuple[int, int]]] = {}
    position = 0
    spans: list[tuple[int, int, int]] = []
    sources = {source.id: source for source in project.sources}
    for index, decision in enumerate(prepared):
        if decision.action != "DROP" and decision.provenance != "user:final_tail":
            end = position + decision.source_end_us - decision.source_start_us
            spans.append((position, end, index))
            position = end
    for chapter in range(chapters):
        chapter_start = total * chapter // chapters
        chapter_end = total * (chapter + 1) // chapters
        midpoint = (chapter_start + chapter_end) // 2
        options: list[tuple[float, int, int, int, int]] = []
        candidates = STORY.candidates if score_window else 1
        for part in range(candidates):
            position = chapter_start + (chapter_end - chapter_start) * (2 * part + 1) // (
                2 * candidates
            )
            left, right, index = next((a, b, i) for a, b, i in spans if a <= position < b)
            decision = prepared[index]
            length = min(STORY.episode_us, right - left)
            start = max(left, min(position - length // 2, right - length))
            source_start = decision.source_start_us + start - left
            source_end = source_start + length
            score = (
                score_window(sources[decision.source_id], source_start, source_end)
                if score_window
                else 0.0
            )
            short_penalty = 2 * (STORY.episode_us - length) / STORY.episode_us
            options.append(
                (score + short_penalty, abs(position - midpoint), index, source_start, source_end)
            )
        _, _, index, source_start, source_end = min(options)
        length = source_end - source_start
        windows.setdefault(index, []).append((source_start, source_start + length))
        if progress:
            progress((chapter + 1) / chapters)

    decisions: list[RenderDecision] = []
    for index, decision in enumerate(prepared):
        if decision.provenance == "user:final_tail":
            decisions.append(decision)
            continue
        selected = windows.get(index, [])
        if not selected or decision.action == "DROP":
            decisions.append(
                decision.model_copy(
                    update={
                        "action": "DROP",
                        "output_frame_count": 0,
                        "provenance": decision.provenance
                        if decision.action == "DROP"
                        else "photoshop:summary_omission",
                    }
                )
            )
            continue
        merged: list[tuple[int, int]] = []
        for start, end in sorted(selected):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        cuts = sorted(
            {decision.source_start_us, decision.source_end_us}
            | {edge for window in merged for edge in window}
        )
        for start, end in zip(cuts, cuts[1:], strict=False):
            keep = any(left <= start and end <= right for left, right in merged)
            decisions.append(
                decision.model_copy(
                    update={
                        "source_start_us": start,
                        "source_end_us": end,
                        "action": "SPEED" if keep else "DROP",
                        "output_frame_count": 1 if keep else 0,
                        "provenance": "photoshop:episode" if keep else "photoshop:summary_omission",
                    }
                )
            )

    return _allocate(project, base, decisions)


def _allocate(project: Project, base: RenderPlan, decisions: list[RenderDecision]) -> RenderPlan:
    fps = base.output_fps
    kept = [d for d in decisions if d.action != "DROP"]
    caps = [max(1, (d.source_end_us - d.source_start_us) * fps // 1_000_000) for d in kept]
    minimum = [
        cap
        if d.provenance == "user:final_tail"
        else max(1, (cap + STORY.max_speed - 1) // STORY.max_speed)
        for d, cap in zip(kept, caps, strict=True)
    ]
    maximum = [cap for cap in caps]
    target = min(base.target_frame_count, sum(caps))
    if sum(minimum) > target:
        # The frame budget is infeasible with this many episodes; preserve readability.
        target = sum(minimum)
    counts = iter(distribute(minimum, [float(cap) for cap in caps], maximum, target))
    result = base.model_copy(
        update={
            "decisions": [
                d.model_copy(update={"output_frame_count": next(counts)})
                if d.action != "DROP"
                else d
                for d in decisions
            ],
            "actual_frame_count": target,
            "warnings": []
            if target == base.target_frame_count
            else ["Readable speed requires a longer output"],
            "settings": base.settings | {"mode": "photoshop_episodes"},
        }
    )
    validate_plan(project, result)
    return result
