"""Select chronological moving drawing episodes using the soundtrack's shot lengths."""

import math
from collections.abc import Callable
from threading import Event

from sculptcut.budget import distribute
from sculptcut.domain import Project, SourceMedia
from sculptcut.media import Cancelled
from sculptcut.music_episodes import SETTINGS
from sculptcut.photoshop_story import STORY
from sculptcut.planning import RenderDecision, RenderPlan, validate_plan


def photoshop_music_plan(
    project: Project,
    base: RenderPlan,
    grid: list[int],
    score_window: Callable[[SourceMedia, int, int], float],
    cancel: Event | None = None,
    progress: Callable[[float], None] | None = None,
) -> RenderPlan:
    """Choose disjoint source windows; never turn an episode into a still or slow motion."""
    spans: list[tuple[int, int, int]] = []
    total = 0
    for index, decision in enumerate(base.decisions):
        if decision.action != "DROP":
            length = decision.source_end_us - decision.source_start_us
            spans.append((total, total + length, index))
            total += length
    if not spans:
        raise ValueError("No retained Photoshop footage")
    fps = base.output_fps
    target = min(base.target_frame_count, max(1, total * fps // 1_000_000))
    if not grid or grid[-1] != target:
        count = min(SETTINGS.maximum_shots, max(1, round(
            target / fps / SETTINGS.preferred_shot_seconds,
        )))
        grid = [target * i // count for i in range(count + 1)]
    count = min(len(grid) - 1, target)
    grid = [grid[i * (len(grid) - 1) // count] for i in range(count + 1)]
    sources = {source.id: source for source in project.sources}
    origins = {index: start for start, _, index in spans}
    windows: dict[int, list[tuple[int, int, int]]] = {}
    # Chapters use retained work time. Source exclusions cannot reappear or consume a chapter.
    for shot, (left_frame, right_frame) in enumerate(zip(grid, grid[1:], strict=False)):
        if cancel and cancel.is_set():
            raise Cancelled("Photoshop music planning cancelled")
        left, right = total * left_frame // target, total * right_frame // target
        available = [(max(left, a), min(right, b), index) for a, b, index in spans
                     if a < right and b > left]
        wanted = max(
            math.ceil((right_frame - left_frame) * 1_000_000 / fps),
            min(STORY.episode_us,
                (right_frame - left_frame) * STORY.max_speed * 1_000_000 // fps),
        )
        options: list[tuple[float, int, int, int]] = []
        for part in range(STORY.candidates):
            position = left + (right - left) * (2 * part + 1) // (2 * STORY.candidates)
            a, b, index = next((span for span in available if span[0] <= position < span[1]),
                               max(available, key=lambda span: span[1] - span[0]))
            if shot == count - 1:
                a, b, index = available[-1]
            length = min(wanted, b - a)
            start = max(a, min(position - length // 2, b - length))
            if shot == count - 1:
                start = b - length
            decision = base.decisions[index]
            source_start = decision.source_start_us + start - origins[index]
            source_end = source_start + length
            score = score_window(sources[decision.source_id], source_start, source_end)
            # Prefer a readable clip if a chapter crosses several small retained ranges.
            penalty = max(0, (right_frame - left_frame) - length * fps / 1_000_000)
            options.append((score + penalty, index, source_start, source_end))
            if shot == count - 1:
                break
        _, index, start, end = min(options)
        windows.setdefault(index, []).append((start, end, right_frame - left_frame))
        if progress:
            progress((shot + 1) / count)

    decisions: list[RenderDecision] = []
    preferred: list[float] = []
    caps: list[int] = []
    for index, decision in enumerate(base.decisions):
        cursor = decision.source_start_us
        for start, end, frames in windows.get(index, []):
            if cursor < start:
                decisions.append(decision.model_copy(update={
                    "source_start_us": cursor, "source_end_us": start, "action": "DROP",
                    "output_frame_count": 0, "provenance": "photoshop:summary_omission",
                }))
            decisions.append(decision.model_copy(update={
                "source_start_us": start, "source_end_us": end, "action": "SPEED",
                "output_frame_count": 1, "provenance": "photoshop:music_episode",
            }))
            preferred.append(float(frames))
            caps.append(max(1, (end - start) * fps // 1_000_000))
            cursor = end
        if cursor < decision.source_end_us:
            decisions.append(decision.model_copy(update={
                "source_start_us": cursor, "action": "DROP", "output_frame_count": 0,
                "provenance": decision.provenance if decision.action == "DROP"
                else "photoshop:summary_omission",
            }))
    minimum = [max(1, math.ceil(cap / STORY.max_speed)) for cap in caps]
    actual = max(sum(minimum), min(target, sum(caps)))
    allocation = iter(distribute(minimum, preferred, caps, actual))
    result = base.model_copy(update={
        "decisions": [d.model_copy(update={"output_frame_count": next(allocation)})
                      if d.output_frame_count else d for d in decisions],
        "actual_frame_count": actual,
        "settings": base.settings | {"mode": "photoshop_music_episodes"},
        "warnings": [] if actual == base.target_frame_count else [
            "Target infeasible with retained Photoshop footage at readable speed",
        ],
    })
    validate_plan(project, result)
    return result
