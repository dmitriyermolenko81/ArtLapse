"""Keep a manually trimmed shorts export at its planned duration."""

import math
from collections.abc import Callable
from pathlib import Path
from threading import Event

from sculptcut.budget import distribute
from sculptcut.domain import Project
from sculptcut.intro import body_start_frame
from sculptcut.music_library import music_signature
from sculptcut.planning import RenderDecision, RenderPlan, validate_plan
from sculptcut.rendering import render
from sculptcut.variants import read_variant


def _merged_cut_frames(
    cuts: list[tuple[int, int]], fps: int, body_frames: int, intro_offset: int
) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for start_us, end_us in sorted(cuts):
        start = start_us * fps // 1_000_000
        end = (end_us * fps + 999_999) // 1_000_000
        if start < intro_offset:
            raise ValueError(
                "Заставку шортса нельзя вырезать этим инструментом. "
                "Выделите участок после заставки."
            )
        start = max(0, min(body_frames, start - intro_offset))
        end = max(0, min(body_frames, end - intro_offset))
        if start >= end:
            continue
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    if not result:
        raise ValueError("Вырезка не затрагивает видеоряд шортса")
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
    project: Project, plan: RenderPlan, cuts: list[tuple[int, int]]
) -> RenderPlan:
    """Remove selected output ranges and refill the original shorts frame budget."""
    if (
        project.settings.preset not in ("shorts", "photoshop_shorts")
        or plan.settings.get("preset") != project.settings.preset
    ):
        raise ValueError("Достройка длительности применяется только к шортсам")
    if any(
        decision.output_frame_count and decision.action not in ("SPEED", "KEEP")
        for decision in plan.decisions
    ):
        raise ValueError("Этот старый монтажный план нельзя безопасно достроить после вырезки")
    offset = body_start_frame(project.settings, plan.output_fps, plan.actual_frame_count)
    if project.settings.presentation_video is not None and any(
        start * plan.output_fps < offset * 1_000_000
        or end * plan.output_fps > (offset + plan.actual_frame_count) * 1_000_000
        for start, end in cuts
    ):
        raise ValueError("Подрезка с достройкой доступна внутри монтажа. "
                         "Презентацию измените кнопкой «Презентация модели».")
    frame_cuts = _merged_cut_frames(
        cuts,
        plan.output_fps,
        plan.actual_frame_count,
        offset,
    )
    decisions: list[RenderDecision] = []
    position = 0
    for decision in plan.decisions:
        decisions.extend(_split_decision(decision, position, frame_cuts))
        position += decision.output_frame_count

    kept = [index for index, decision in enumerate(decisions) if decision.output_frame_count]
    minimum = [decisions[index].output_frame_count for index in kept]
    maximum = [
        max(
            decisions[index].output_frame_count,
            math.floor(
                (decisions[index].source_end_us - decisions[index].source_start_us)
                * plan.output_fps
                / 1_000_000
            ),
        )
        for index in kept
    ]
    target = plan.target_frame_count
    if not kept or sum(maximum) < target:
        raise ValueError(
            "После вырезки недостаточно исходного материала для полного минутного шортса "
            "без повторов или замедления."
        )
    counts = distribute(minimum, [float(value) for value in minimum], maximum, target)
    for index, count in zip(kept, counts, strict=True):
        decisions[index] = decisions[index].model_copy(update={"output_frame_count": count})
    removed_frames = sum(end - start for start, end in frame_cuts)
    settings = dict(plan.settings)
    settings.pop("request_key", None)
    settings["manual_cut_seconds"] = removed_frames / plan.output_fps
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
            + ["Manual cuts retained; remaining shorts footage expanded to the target"],
        }
    )
    validate_plan(project, result)
    return result


def rebuild_short_after_cuts(
    root: Path,
    video: Path,
    cuts: list[tuple[int, int]],
    cancel: Event,
    progress: Callable[[float], None],
    *,
    encoder: str = "auto",
) -> Path:
    """Render a new full-duration shorts variant without repeating analysis."""
    state = read_variant(video)
    if not state.get("project") or not state.get("plan"):
        raise ValueError(
            "У выбранного шортса нет сохранённого монтажного плана. "
            "Его можно подрезать, но автоматически достроить до минуты нельзя."
        )
    if state.get("operation") == "cut":
        raise ValueError("Выберите исходную готовую версию шортса, а не подрезанную копию")
    snapshot_project = Project.model_validate(state["project"])
    snapshot_plan = RenderPlan.model_validate(state["plan"])
    if snapshot_project.settings.preset not in ("shorts", "photoshop_shorts"):
        raise ValueError("Выбранный ролик не является шортсом")

    saved_music: Path | None = None
    music_state = state.get("music")
    if isinstance(music_state, dict) and music_state.get("path"):
        saved_music = (video.parent / str(music_state["path"])).resolve()
        if not saved_music.is_file():
            raise ValueError("Сохранённая музыка этого варианта не найдена.")
    render_settings = snapshot_project.settings.model_copy(
        update={"music_path": saved_music, "music_folder": None}
    )
    render_project = snapshot_project.model_copy(update={"settings": render_settings})
    plan_settings = dict(snapshot_plan.settings)
    plan_settings["music_fingerprint"] = music_signature(render_settings)
    snapshot_plan = snapshot_plan.model_copy(update={"settings": plan_settings})
    rebuilt = refill_plan_after_cuts(render_project, snapshot_plan, cuts)

    index = 1
    destination = video.with_name(f"{video.stem}-edit-{index:03d}.mp4")
    while destination.exists():
        index += 1
        destination = video.with_name(f"{video.stem}-edit-{index:03d}.mp4")
    return render(
        root,
        render_project,
        rebuilt,
        destination,
        cancel,
        encoder=encoder,
        progress=progress,
        variant_project=snapshot_project,
        variant_parent=video,
        variant_operation="shorts_refill",
        variant_cuts=cuts,
    )
