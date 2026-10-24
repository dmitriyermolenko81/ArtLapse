"""Restore the planned duration and bookends after manual cuts in any preset."""

import math
from collections.abc import Callable
from pathlib import Path
from threading import Event

from sculptcut.budget import distribute
from sculptcut.domain import Project
from sculptcut.intro import body_start_frame, output_frame_count
from sculptcut.music_library import music_signature
from sculptcut.planning import RenderDecision, RenderPlan, validate_plan
from sculptcut.rendering import render
from sculptcut.variants import cut_history, read_variant


def _merged_cut_frames(
    cuts: list[tuple[int, int]], fps: int, body_frames: int, intro_offset: int
) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for start_us, end_us in sorted(cuts):
        start = start_us * fps // 1_000_000
        end = (end_us * fps + 999_999) // 1_000_000
        start = max(0, min(body_frames, start - intro_offset))
        end = max(0, min(body_frames, end - intro_offset))
        if start >= end:
            continue
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result


def cuts_on_plan_clock(
    video: Path, project: Project, plan: RenderPlan, cuts: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Map successive ordinary trims back to the saved plan, including bookends."""
    from sculptcut.output_cuts import retained_ranges, selected_ranges

    duration = output_frame_count(project.settings, plan.output_fps, plan.actual_frame_count)
    duration_us = (duration * 1_000_000 + plan.output_fps - 1) // plan.output_fps
    ranges = [(0, duration_us)]
    bookends = [tuple(cut) for cut in read_variant(video).get("bookend_cuts_us", [])]
    for batch in [*([bookends] if bookends else []), *cut_history(video), cuts]:
        remaining_us = sum(b - a for a, b in ranges)
        # The player's millisecond duration can round up past the exact last
        # frame. Clamp only that rounded endpoint, keeping other bounds strict.
        player_end_us = (remaining_us + 999) // 1000 * 1000
        normalized = [(start, min(end, remaining_us) if end <= player_end_us else end)
                      for start, end in batch]
        kept = retained_ranges(remaining_us, normalized)
        ranges = [interval for a, b in kept for interval in selected_ranges(ranges, a, b)]
    removed = []
    cursor = 0
    for start, end in ranges:
        if cursor < start:
            removed.append((cursor, start))
        cursor = end
    if cursor < duration_us:
        removed.append((cursor, duration_us))
    return removed


def refill_render_context(
    video: Path, project: Project, plan: RenderPlan,
) -> tuple[Project, RenderPlan]:
    """Use the exported soundtrack even when its original library has changed."""
    from sculptcut.output_cuts import saved_soundtrack

    soundtrack = saved_soundtrack(video)
    settings = project.settings.model_copy(update={
        "music_path": soundtrack[0] if soundtrack else None, "music_folder": None,
    })
    return (
        project.model_copy(update={"settings": settings}),
        plan.model_copy(update={"settings": plan.settings | {
            "music_fingerprint": music_signature(settings),
        }}),
    )


def _expand_montage(decision: RenderDecision) -> list[RenderDecision]:
    """Use the same sample/frame boundaries as the renderer, preserving source coverage."""
    if decision.action != "MONTAGE":
        return [decision]
    samples = decision.sample_times_us
    result = []
    if samples[0] > decision.source_start_us:
        result.append(decision.model_copy(update={
            "action": "DROP", "source_end_us": samples[0], "output_frame_count": 0,
            "sample_times_us": [],
        }))
    for index, start in enumerate(samples):
        result.append(decision.model_copy(update={
            "action": "HOLD", "source_start_us": start,
            "source_end_us": samples[index + 1] if index + 1 < len(samples)
            else decision.source_end_us,
            "output_frame_count": decision.output_frame_count * (index + 1) // len(samples)
            - decision.output_frame_count * index // len(samples),
            "sample_times_us": [], "provenance": "user:refill_montage",
        }))
    return result


def _split_decision(
    decision: RenderDecision,
    output_start: int,
    cuts: list[tuple[int, int]],
) -> list[RenderDecision]:
    count = decision.output_frame_count
    if not count:
        return [decision]
    output_end = output_start + count
    if decision.action == "HOLD":
        # A hold always displays the same source timestamp. Splitting its source
        # interval proportionally would silently replace that frame with another.
        removed = sum(max(0, min(output_end, end) - max(output_start, start))
                      for start, end in cuts)
        return [decision.model_copy(update={
            "output_frame_count": count - removed,
            **({"provenance": "user:shorts_partial_cut"} if 0 < removed < count else {}),
            **({"action": "DROP", "provenance": "user:shorts_manual_cut",
                "sample_times_us": []} if removed == count else {}),
        })]
    boundaries = {0, count}
    for start, end in cuts:
        if start < output_end and end > output_start:
            boundaries.update(
                (
                    max(0, start - output_start),
                    min(count, end - output_start),
                )
            )
    ordered = sorted(boundaries)
    duration = decision.source_end_us - decision.source_start_us
    result = []
    for left, right in zip(ordered, ordered[1:], strict=False):
        source_start = decision.source_start_us + duration * left // count
        source_end = decision.source_start_us + duration * right // count
        if source_start >= source_end:
            continue
        removed = any(
            start < output_start + right and end > output_start + left for start, end in cuts
        )
        result.append(
            decision.model_copy(
                update={
                    "source_start_us": source_start,
                    "source_end_us": source_end,
                    "action": "DROP" if removed else decision.action,
                    "output_frame_count": 0 if removed else right - left,
                    "provenance": "user:shorts_manual_cut"
                    if removed
                    else decision.provenance,
                    "sample_times_us": [] if removed else decision.sample_times_us,
                }
            )
        )
    return result


def refill_plan_after_cuts(
    project: Project, plan: RenderPlan, cuts: list[tuple[int, int]], *, allow_empty: bool = False,
    respect_episode_speeds: bool = False,
) -> RenderPlan:
    """Retain body exclusions, restore complete bookends, and refill the body budget."""
    validate_plan(project, plan)
    total = output_frame_count(project.settings, plan.output_fps, plan.actual_frame_count)
    if (not cuts and not allow_empty) or any(not 0 <= start < end or end * plan.output_fps >
                       total * 1_000_000 + plan.output_fps - 1 for start, end in cuts):
        raise ValueError("Неверные границы удаляемого участка")
    offset = body_start_frame(project.settings, plan.output_fps, plan.actual_frame_count)
    frame_cuts = _merged_cut_frames(
        cuts,
        plan.output_fps,
        plan.actual_frame_count,
        offset,
    )
    decisions: list[RenderDecision] = []
    position = 0
    for decision in [part for original in plan.decisions for part in _expand_montage(original)]:
        decisions.extend(_split_decision(decision, position, frame_cuts))
        position += decision.output_frame_count

    kept = [index for index, decision in enumerate(decisions) if decision.output_frame_count]
    minimum = [decisions[index].output_frame_count for index in kept]
    from sculptcut.beat_shorts import SETTINGS as STILL_SETTINGS

    maximum = [
        minimum[position] if respect_episode_speeds and decisions[index].provenance in
        ("user:SET_SPEED", "user:KEEP", "user:LOCK_SEGMENT", "user:FORCE_MONTAGE") else
        (minimum[position] if decisions[index].provenance in
         ("user:final_tail", "user:shorts_final") else
         max(minimum[position], round(STILL_SETTINGS.maximum_hold_seconds * plan.output_fps)))
        if decisions[index].action == "HOLD" else
        max(
            decisions[index].output_frame_count,
            math.floor(
                (decisions[index].source_end_us - decisions[index].source_start_us)
                * plan.output_fps
                / 1_000_000
            ),
        )
        for position, index in enumerate(kept)
    ]
    target = (plan.target_frame_count if respect_episode_speeds
              else max(plan.target_frame_count, plan.actual_frame_count))
    if sum(minimum) > target:
        raise ValueError("Заданные скорости эпизодов не позволяют сохранить длительность. "
                         "Увеличьте длительность или ускорьте эпизоды.")
    if not kept or sum(maximum) < target:
        raise ValueError(
            "После вырезки недостаточно исходного материала, чтобы сохранить "
            "запланированную длительность без повторов или замедления."
        )
    counts = distribute(minimum, [float(value) for value in minimum], maximum, target)
    for index, count in zip(kept, counts, strict=True):
        decision = decisions[index]
        decisions[index] = decision.model_copy(update={
            "output_frame_count": count,
            "action": "SPEED" if decision.action == "KEEP"
            and count != decision.output_frame_count else decision.action,
        })
    removed_frames = sum(end - start for start, end in frame_cuts)
    settings = dict(plan.settings)
    settings.pop("request_key", None)
    settings["manual_cut_seconds"] = removed_frames / plan.output_fps
    settings["actual_output_frames"] = output_frame_count(
        project.settings, plan.output_fps, target,
    )
    if settings.get("story") == "process":
        settings["final_view_frames"] = sum(
            decision.output_frame_count
            for decision in decisions
            if decision.provenance == "user:shorts_final"
        )
    result = plan.model_copy(
        update={
            "decisions": decisions,
            "actual_frame_count": target,
            "settings": settings,
            "warnings": list(plan.warnings)
            + ["Manual cuts retained; bookends restored and remaining footage expanded to target"],
        }
    )
    validate_plan(project, result)
    return result


def rebuild_after_cuts(
    root: Path,
    video: Path,
    cuts: list[tuple[int, int]],
    cancel: Event,
    progress: Callable[[float], None],
    *,
    encoder: str = "auto",
) -> Path:
    """Render a new full-duration variant from originals without repeating analysis."""
    state = read_variant(video)
    if not state.get("project") or not state.get("plan"):
        raise ValueError(
            "У выбранного ролика нет сохранённого монтажного плана. "
            "Его можно подрезать, но автоматически восстановить "
            "запланированную длительность нельзя."
        )
    from sculptcut.combined_edits import prepare_refill_edit

    edit = prepare_refill_edit(root, video, cuts)
    snapshot_project, render_project, snapshot_plan = edit.snapshot, edit.project, edit.plan

    index = 1
    destination = video.with_name(f"{video.stem}-edit-{index:03d}.mp4")
    while destination.exists():
        index += 1
        destination = video.with_name(f"{video.stem}-edit-{index:03d}.mp4")
    return render(
        root,
        render_project,
        snapshot_plan,
        destination,
        cancel,
        encoder=encoder,
        progress=progress,
        variant_project=snapshot_project,
        variant_parent=video,
        variant_operation="duration_refill",
        variant_cuts=edit.cuts,
        variant_episode_base=edit.base_plan,
    )


# Keep the old Python entry point for callers and existing integrations.
rebuild_short_after_cuts = rebuild_after_cuts
