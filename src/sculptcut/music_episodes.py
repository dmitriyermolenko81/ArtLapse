"""Retain continuous Shorts episodes and time their cuts against the soundtrack."""

import math
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from sculptcut.audio_beat_sync import MusicEvent, analyze_music, select_cut_beats
from sculptcut.domain import Project
from sculptcut.intro import body_start_frame, output_frame_count
from sculptcut.media import Cancelled
from sculptcut.music_library import soundtrack
from sculptcut.planning import RenderPlan, validate_plan
from sculptcut.process_story import STORY


@dataclass(frozen=True)
class MusicEpisodeSettings:
    preferred_shot_seconds: float = 1.5
    maximum_shots: int = 300
    maximum_cut_shift_seconds: float = 2.5
    missed_accent_cost: float = 16.0
    duration_change_cost: float = 0.25


SETTINGS = MusicEpisodeSettings()


def episode_grid(
    project: Project, frame_count: int, events: list[MusicEvent], music_offset: float = 0.0,
) -> list[int]:
    """Return actual accent frames, without inventing cuts between musical events."""
    fps = project.settings.output_fps
    minimum = max(1, math.ceil(project.settings.beat_sync_min_shot_seconds * fps))
    frames = sorted({round((e.time_seconds - music_offset) * fps) for e in events})
    cuts: list[int] = []
    for frame in frames:
        if frame - (cuts[-1] if cuts else 0) >= minimum and frame_count - frame >= minimum:
            cuts.append(frame)
    if not cuts:
        return []
    limit = SETTINGS.maximum_shots - 1
    if len(cuts) > limit:
        cuts = [cuts[i * len(cuts) // limit] for i in range(limit)]
    return [0, *cuts, frame_count]


def music_events(
    root: Path, project: Project, frame_count: int, cancel: Event | None,
) -> tuple[list[MusicEvent], float]:
    settings = project.settings
    if not settings.music_path and not settings.music_folder:
        raise ValueError("Для эпизодов под музыку выберите музыку.")
    if not settings.beat_sync_enabled or settings.beat_sync_strength <= 0:
        return [], 0.0
    fps = settings.output_fps
    duration = output_frame_count(settings, fps, frame_count) / fps
    music = soundtrack(root, settings, duration, cancel)
    assert music is not None
    analysis = analyze_music(
        music, duration, root / "music_analysis_cache",
        loop=settings.music_loop and settings.music_folder is None, cancel=cancel,
    )
    events = select_cut_beats(
        analysis, settings.beat_sync_mode, settings.beat_sync_min_shot_seconds,
        settings.beat_sync_max_shot_seconds, strength=settings.beat_sync_strength,
    )
    return events, body_start_frame(settings, fps, frame_count) / fps


def music_episode_grid(
    root: Path, project: Project, frame_count: int, cancel: Event | None,
) -> list[int]:
    events, offset = music_events(root, project, frame_count, cancel)
    return episode_grid(project, frame_count, events, offset)


def fit_episode_grid(project: Project, plan: RenderPlan, grid: list[int]) -> RenderPlan | None:
    """Fit automatic source windows to music slots; locks require conservative retiming."""
    from sculptcut.planning import RenderDecision

    active = [d for d in plan.decisions if d.output_frame_count]
    if not active or len(active) >= len(grid) or any(
        d.action != "SPEED" or d.provenance not in ("user:process_story", "user:shorts_final")
        for d in active
    ):
        return None
    # Rejected visual candidates may leave fewer episodes. Skip beats rather than
    # inserting a non-musical boundary or returning excluded material.
    selected = list(range(len(active)))
    while selected:
        ends = [grid[i * (len(grid) - 1) // len(selected)] for i in range(len(selected) + 1)]
        counts = [b - a for a, b in zip(ends, ends[1:], strict=False)]
        feasible = [index for index, n in zip(selected, counts, strict=True)
                    if (active[index].source_end_us - active[index].source_start_us)
                    * plan.output_fps // 1_000_000 >= n]
        if feasible == selected:
            break
        selected = feasible
    if len(selected) < 2:
        return None
    allocation = iter(counts)
    retained = {id(active[i]) for i in selected}
    decisions: list[RenderDecision] = []
    for decision in plan.decisions:
        if not decision.output_frame_count:
            decisions.append(decision)
            continue
        if id(decision) not in retained:
            decisions.append(decision.model_copy(update={
                "action": "DROP", "output_frame_count": 0,
                "provenance": "auto:music_short_omission",
            }))
            continue
        count = next(allocation)
        start, end = decision.source_start_us, decision.source_end_us
        maximum_us = count * STORY.max_speed * 1_000_000 // plan.output_fps
        if end - start > maximum_us:
            start = end - maximum_us if decision.provenance == "user:shorts_final" else (
                start + (end - start - maximum_us) // 2
            )
            end = start + maximum_us
        for a, b, keep in ((decision.source_start_us, start, False),
                           (start, end, True), (end, decision.source_end_us, False)):
            if a < b:
                decisions.append(decision.model_copy(update={
                    "source_start_us": a, "source_end_us": b,
                    "action": "SPEED" if keep else "DROP",
                    "output_frame_count": count if keep else 0,
                    "provenance": decision.provenance if keep else "auto:music_trim",
                }))
    return plan.model_copy(update={"decisions": decisions})


def describe_sync(project: Project, plan: RenderPlan, beats: dict[int, float]) -> RenderPlan:
    end = aligned = 0
    active = [d for d in plan.decisions if d.output_frame_count]
    for decision in active[:-1]:
        end += decision.output_frame_count
        aligned += int(end in beats)
    total = max(0, len(active) - 1)
    status = "on" if aligned == total and total else "partial" if beats else "unavailable"
    result = plan.model_copy(update={"settings": plan.settings | {
        "story": "music_episodes", "beat_sync": status,
        "beat_sync_aligned_cuts": aligned, "beat_sync_total_cuts": total,
        "final_view_frames": sum(d.output_frame_count for d in active
                                 if d.provenance == "user:shorts_final"),
    }})
    validate_plan(project, result)
    return result


def retime_episodes(
    project: Project, plan: RenderPlan, events: list[MusicEvent],
    music_offset: float = 0.0, cancel: Event | None = None,
) -> RenderPlan:
    """Fit the music grid first; preserve manual speed locks when exact fitting fails."""
    if cancel and cancel.is_set():
        raise Cancelled("Music episode planning cancelled")
    active = [i for i, d in enumerate(plan.decisions) if d.output_frame_count]
    fps = plan.output_fps
    beats: dict[int, float] = {}
    for event in events:
        frame = round((event.time_seconds - music_offset) * fps)
        if 0 < frame < plan.actual_frame_count:
            beats[frame] = max(beats.get(frame, 0.0), event.strength)
    if not beats or len(active) < 2:
        return describe_sync(project, plan, beats)
    grid = episode_grid(project, plan.actual_frame_count, events, music_offset)
    fitted = fit_episode_grid(project, plan, grid) if grid else None
    if fitted is not None:
        return describe_sync(project, fitted, beats)
    originals = [plan.decisions[i].output_frame_count for i in active]
    bounds: list[tuple[int, int]] = []
    for index, count in zip(active, originals, strict=True):
        decision = plan.decisions[index]
        if decision.action != "SPEED" or decision.provenance in (
            "user:KEEP", "user:RETAIN", "user:SET_SPEED", "user:LOCK_SEGMENT",
        ):
            bounds.append((count, count))
            continue
        natural = max(1, (decision.source_end_us - decision.source_start_us) * fps // 1_000_000)
        low = max(math.ceil(natural / STORY.max_speed),
                  round(project.settings.beat_sync_min_shot_seconds * fps), 1)
        high = natural
        bounds.append((min(count, low), max(count, high)))

    # Each layer represents an existing episode, each state its cumulative end.
    # A small local window bounds work independently of source recording length.
    radius = max(1, round(SETTINGS.maximum_cut_shift_seconds * fps))
    costs = {0: 0.0}
    parents: list[dict[int, int]] = []
    original_end = 0
    for shot, (count, (low, high)) in enumerate(zip(originals, bounds, strict=True)):
        if cancel and cancel.is_set():
            raise Cancelled("Music episode planning cancelled")
        original_end += count
        last = shot == len(active) - 1
        candidates = {original_end} if last else {original_end} | {
            frame for frame in beats if abs(frame - original_end) <= radius
        }
        next_costs: dict[int, float] = {}
        previous_ends: dict[int, int] = {}
        for end in sorted(candidates):
            boundary_cost = 0.0 if last else (
                (end - original_end) ** 2 / radius ** 2
                + (1 - min(1.0, beats[end])) if end in beats else SETTINGS.missed_accent_cost
            )
            options = [
                (cost + boundary_cost + SETTINGS.duration_change_cost
                 * ((end - start - count) / max(1, count)) ** 2, start)
                for start, cost in costs.items() if low <= end - start <= high
            ]
            if options:
                next_costs[end], previous_ends[end] = min(options)
        costs = next_costs
        parents.append(previous_ends)
    end = plan.actual_frame_count
    counts = originals[:]
    for shot in reversed(range(len(active))):
        start = parents[shot][end]
        counts[shot] = end - start
        end = start
    decisions = list(plan.decisions)
    for index, count in zip(active, counts, strict=True):
        decisions[index] = decisions[index].model_copy(update={"output_frame_count": count})
    return describe_sync(project, plan.model_copy(update={"decisions": decisions}), beats)


def sync_music_episodes(
    root: Path, project: Project, plan: RenderPlan, cancel: Event | None = None,
) -> RenderPlan:
    """Analyze the exact rendered soundtrack, including intro and library selection."""
    settings = project.settings
    if not settings.beat_sync_enabled or settings.beat_sync_strength <= 0:
        return plan.model_copy(update={"settings": plan.settings | {
            "story": "music_episodes", "beat_sync": "disabled",
        }})
    events, offset = music_events(root, project, plan.actual_frame_count, cancel)
    return retime_episodes(project, plan, events, offset, cancel)
