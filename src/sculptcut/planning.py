"""Deterministic integer-frame M0 planning and source coverage validation."""

import logging
import math
from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import Literal

from pydantic import Field

from sculptcut.analysis import analysis_key
from sculptcut.budget import distribute
from sculptcut.development import development_key
from sculptcut.domain import Model, Project, Roi, SourceMedia, SubjectCamera
from sculptcut.idle import IdleRange, project_idle
from sculptcut.intro import body_frame_budget, intro_signature, output_frame_count
from sculptcut.music_library import music_signature
from sculptcut.project import atomic_write, cache_key, database, validate_sources
from sculptcut.screen_cache import screen_check_cache
from sculptcut.segmentation import SEGMENT_VERSION, Segment
from sculptcut.shorts_layout import MUSIC_SHORTS_PRESETS, PHOTOSHOP_PRESETS, SHORTS_PRESETS
from sculptcut.webcam import webcam_roi


class RenderDecision(Model):
    source_id: str
    source_start_us: int = Field(ge=0)
    source_end_us: int = Field(gt=0)
    action: Literal["DROP", "SPEED", "KEEP", "HOLD", "MONTAGE"]
    output_frame_count: int = Field(ge=0)
    transition: Literal["cut"] = "cut"
    crop: Roi | Literal["full"] = "full"
    provenance: str
    sample_times_us: list[int] = Field(default_factory=list)
    subject_camera: SubjectCamera | None = None


class RenderPlan(Model):
    schema_version: Literal[1] = 1
    source_fingerprints: dict[str, str]
    settings: dict[str, str | int | float]
    output_fps: int = Field(gt=0)
    target_frame_count: int = Field(gt=0)
    decisions: list[RenderDecision]
    actual_frame_count: int = Field(gt=0)
    warnings: list[str] = Field(default_factory=list)


def allocate_frames(durations: list[int], target: int, fps: int) -> list[int]:
    caps = [max(1, math.floor(d * fps / 1_000_000)) for d in durations]
    if target < len(durations) or target > sum(caps):
        raise ValueError("Frame budget cannot preserve all ranges at >=1x")
    # Largest-remainder allocation with per-range capacities and at least one frame.
    allocation = [1] * len(durations)
    remaining = target - len(durations)
    while remaining:
        active = [i for i in range(len(durations)) if allocation[i] < caps[i]]
        total = sum(durations[i] for i in active)
        quotas = {i: remaining * durations[i] / total for i in active}
        used = 0
        for i in active:
            amount = min(caps[i] - allocation[i], math.floor(quotas[i]))
            allocation[i] += amount
            used += amount
        remaining -= used
        if remaining:
            for i in sorted(active, key=lambda i: (-(quotas[i] % 1), i)):
                if allocation[i] < caps[i]:
                    allocation[i] += 1
                    remaining -= 1
                    if not remaining:
                        break
    return allocation


def build_plan(project: Project, idle: list[IdleRange], target_seconds: float) -> RenderPlan:
    if not math.isfinite(target_seconds) or target_seconds <= 0:
        raise ValueError("Target duration must be finite and positive")
    decisions = []
    for source in project.sources:
        position = 0
        for interval in sorted(
            (r for r in idle if r.source_id == source.id), key=lambda r: r.start_us
        ):
            if not position <= interval.start_us < interval.end_us <= source.duration_us:
                raise ValueError("Invalid idle interval")
            if interval.start_us > position:
                decisions.append(
                    RenderDecision(
                        source_id=source.id,
                        source_start_us=position,
                        source_end_us=interval.start_us,
                        action="SPEED",
                        output_frame_count=1,
                        provenance="retain_unknown",
                    )
                )
            decisions.append(
                RenderDecision(
                    source_id=source.id,
                    source_start_us=interval.start_us,
                    source_end_us=interval.end_us,
                    action="DROP",
                    output_frame_count=0,
                    provenance=interval.reason,
                )
            )
            position = interval.end_us
        if position < source.duration_us:
            decisions.append(
                RenderDecision(
                    source_id=source.id,
                    source_start_us=position,
                    source_end_us=source.duration_us,
                    action="SPEED",
                    output_frame_count=1,
                    provenance="retain_unknown",
                )
            )
    kept = [d for d in decisions if d.action != "DROP"]
    if not kept:
        raise ValueError("Plan contains no retained footage")
    durations = [d.source_end_us - d.source_start_us for d in kept]
    fps = project.settings.output_fps
    requested = max(1, round(target_seconds * fps))
    capacity = sum(max(1, math.floor(d * fps / 1_000_000)) for d in durations)
    actual = min(capacity, max(len(kept), requested))
    counts = iter(allocate_frames(durations, actual, fps))
    updated = [
        d.model_copy(update={"output_frame_count": next(counts)}) if d.action != "DROP" else d
        for d in decisions
    ]
    plan = RenderPlan(
        source_fingerprints={s.id: s.fingerprint for s in project.sources},
        settings={
            "mode": "full_ui",
            "preset": project.settings.preset,
            "target_seconds": target_seconds,
            "audio": "music"
            if project.settings.music_path or project.settings.music_folder
            else "muted",
            "music_fingerprint": music_signature(project.settings),
            "intro_fingerprint": intro_signature(project.settings),
        },
        output_fps=fps,
        target_frame_count=requested,
        decisions=updated,
        actual_frame_count=actual,
        warnings=[] if actual == requested else ["Target infeasible; preserved ranges at >=1x"],
    )
    validate_plan(project, plan)
    return plan


def simple_speed_plan(project: Project, target_seconds: float) -> RenderPlan:
    """Retain every source range and distribute one proportional output-time budget."""
    plan = build_plan(project, [], target_seconds)
    return plan.model_copy(
        update={
            "decisions": [
                decision.model_copy(update={"provenance": "preset:simple_speed"})
                for decision in plan.decisions
            ],
            "settings": plan.settings | {"mode": "simple_speed"},
        }
    )


def validate_plan(project: Project, plan: RenderPlan) -> None:
    if plan.settings.get("preset", "timelapse") != project.settings.preset:
        raise ValueError("Preset changed; rebuild the plan")
    music_hash = music_signature(project.settings)
    if plan.settings.get("music_fingerprint", "") != music_hash:
        raise ValueError("Music changed; rebuild the plan")
    if plan.settings.get("intro_fingerprint", "") != intro_signature(project.settings):
        if project.settings.presentation_video or "presentation-1" in str(
            plan.settings.get("intro_fingerprint", "")
        ):
            raise ValueError("Заставка или видео-презентация изменились. "
                             "Создайте монтажный план заново.")
        raise ValueError("Intro image changed; rebuild the plan")
    if plan.source_fingerprints != {s.id: s.fingerprint for s in project.sources}:
        raise ValueError("Plan source fingerprints do not match project")
    index = 0
    for source in project.sources:
        position = 0
        while index < len(plan.decisions) and plan.decisions[index].source_id == source.id:
            decision = plan.decisions[index]
            if not (
                decision.source_start_us == position < decision.source_end_us <= source.duration_us
            ):
                raise ValueError("Plan has gaps, overlaps or invalid source bounds")
            if (decision.action == "DROP") != (decision.output_frame_count == 0):
                raise ValueError("Invalid action frame count")
            if decision.action not in ("DROP", "SPEED", "KEEP", "HOLD", "MONTAGE"):
                raise ValueError("Unsupported render action")
            if decision.action == "MONTAGE":
                samples = decision.sample_times_us
                if not (
                    2 <= len(samples) <= decision.output_frame_count
                    and samples == sorted(set(samples))
                    and all(decision.source_start_us <= t < decision.source_end_us for t in samples)
                ):
                    raise ValueError("Invalid montage state timestamps")
            if decision.action == "HOLD" and not (
                decision.provenance.startswith("user:")
                or (
                    project.settings.preset in ("beat_shorts", "photoshop_beat_shorts")
                    and decision.provenance == "auto:beat_still"
                    and (
                        decision.subject_camera is not None
                        or (
                            project.settings.shorts_layout == "square"
                            and decision.source_id in project.settings.shorts_square_rois
                            and isinstance(decision.crop, Roi)
                        )
                    )
                )
            ):
                raise ValueError("Hold requires a verified frame or a beat-shorts still")
            available = max(
                1,
                math.floor(
                    (decision.source_end_us - decision.source_start_us)
                    * plan.output_fps
                    / 1_000_000
                ),
            )
            if decision.action != "HOLD" and decision.output_frame_count > available:
                raise ValueError("Plan silently slows footage below 1x")
            position = decision.source_end_us
            index += 1
        if position != source.duration_us:
            raise ValueError("Plan does not cover every source")
    if index != len(plan.decisions):
        raise ValueError("Unknown or out-of-order source")
    if sum(d.output_frame_count for d in plan.decisions) != plan.actual_frame_count:
        raise ValueError("Plan frame count mismatch")


def weighted_plan(project: Project, base: RenderPlan, segments: list[Segment]) -> RenderPlan:
    decisions = []
    minima, maxima, preferences = [], [], []
    factor = {"calm": 0.5, "balanced": 1.0, "dynamic": 2.0}[project.settings.style]
    for decision in base.decisions:
        if decision.action == "DROP":
            decisions.append(decision)
            continue
        relevant = [
            s
            for s in segments
            if s.source_id == decision.source_id
            and s.start_us < decision.source_end_us
            and s.end_us > decision.source_start_us
        ]
        for segment in relevant:
            start, end = (
                max(segment.start_us, decision.source_start_us),
                min(segment.end_us, decision.source_end_us),
            )
            duration_frames = (end - start) * base.output_fps / 1e6
            max_speed = (
                32 if segment.kind in ("UNKNOWN", "MIXED", "SCULPT", "FINAL_VIEW") else 128
            ) * factor
            minimum = max(1, math.ceil(duration_frames / max_speed))
            maximum = max(1, math.floor(duration_frames))
            minima.append(min(minimum, maximum))
            maxima.append(maximum)
            preferences.append(max(1, duration_frames * (0.2 + segment.importance) / 8))
            decisions.append(
                decision.model_copy(
                    update={
                        "source_start_us": start,
                        "source_end_us": end,
                        "provenance": f"{segment.kind}:importance={segment.importance:.3f}",
                    }
                )
            )
    requested = base.target_frame_count
    actual = max(sum(minima), min(sum(maxima), requested))
    counts = iter(distribute(minima, preferences, maxima, actual))
    updated = [
        d.model_copy(update={"output_frame_count": next(counts)}) if d.action != "DROP" else d
        for d in decisions
    ]
    plan = base.model_copy(
        update={
            "decisions": updated,
            "actual_frame_count": actual,
            "warnings": []
            if actual == requested
            else ["Target infeasible under speed/retention constraints"],
        }
    )
    validate_plan(project, plan)
    return plan


@screen_check_cache()
def plan_project(
    root: Path,
    project: Project,
    target_seconds: float,
    cancel: Event | None = None,
    progress: Callable[[float], None] | None = None,
    stage: Callable[[str], None] | None = None,
) -> RenderPlan:
    from sculptcut.bookends import present_final_result
    from sculptcut.media import Cancelled
    from sculptcut.shorts_layout import apply_square_layout, uses_square

    def phase(message: str) -> None:
        if stage:
            stage(message)
        else:
            logging.getLogger("artlapse").info(message, extra={"event": "planning_stage"})

    if cancel and cancel.is_set():
        raise Cancelled("Planning cancelled")
    total_target_seconds = target_seconds
    target_seconds = body_frame_budget(
        project.settings, project.settings.output_fps, target_seconds,
    ) / project.settings.output_fps
    validate_sources(root, project)
    from sculptcut.overrides import apply_overrides, exclude_dropped

    project_identity = project.model_dump(mode="json")
    project_identity["shared_episode_edits"] = "shared-source-edits-141"
    if project.settings.preset in ("timelapse", "shorts", "beat_shorts", "music_shorts"):
        project_identity["settings"]["segmentation_version"] = SEGMENT_VERSION
    if project.settings.preset in ("timelapse", "shorts", "music_shorts"):
        project_identity["settings"]["development_version"] = development_key()
    if (
        project.settings.preset not in SHORTS_PRESETS
        or project.settings.shorts_layout == "auto"
    ):
        for key in ("shorts_layout", "shorts_square_rois", "square_tracking_fps",
                    "square_tracking_dead_zone", "square_tracking_margin"):
            project_identity["settings"].pop(key, None)
    if project.settings.preset not in MUSIC_SHORTS_PRESETS:
        for key in (
            "beat_sync_enabled",
            "beat_sync_mode",
            "beat_sync_strength",
            "beat_sync_min_shot_seconds",
            "beat_sync_max_shot_seconds",
        ):
            project_identity["settings"].pop(key, None)
    if uses_square(project.settings):
        project_identity["settings"]["square_camera_version"] = (
            "horizontal-208-fixed-music-episodes"
            if project.settings.preset in ("music_shorts", "photoshop_music_shorts")
            else "horizontal-2-viewport"
        )
    if project.settings.preset in ("music_shorts", "photoshop_music_shorts"):
        from dataclasses import asdict

        from sculptcut.music_episodes import SETTINGS as MUSIC_EPISODES

        project_identity["settings"]["music_episodes"] = asdict(MUSIC_EPISODES)
        if project.settings.preset == "photoshop_music_shorts":
            from sculptcut.photoshop_story import STORY as DRAWING_STORY

            project_identity["settings"]["photoshop_episodes"] = asdict(DRAWING_STORY)
    if project.settings.preset == "timelapse":
        from dataclasses import asdict

        from sculptcut.timelapse_cleanup import SETTINGS

        project_identity["settings"]["timelapse_cleanup"] = asdict(SETTINGS)
    request_key = cache_key(
        "total-duration-207-v2",
        "plan-206-photoshop-music-episodes"
        if project.settings.preset == "photoshop_music_shorts"
        else "plan-146-music-first-episodes"
        if project.settings.preset == "music_shorts"
        else "plan-100-photoshop-beat-fixed-camera"
        if project.settings.preset == "photoshop_beat_shorts"
        else "plan-67-canvas-edge-shorts"
        if project.settings.preset == "shorts"
        else "plan-100-photoshop-fixed-camera"
        if project.settings.preset == "photoshop_shorts"
        else "plan-116-global-process-budget"
        if project.settings.preset == "timelapse"
        else "plan-79-continuous-beat-shorts"
        if project.settings.preset == "beat_shorts"
        else "plan-64-visual-diversity",
        project_identity,
        target_seconds,
        music_signature(project.settings),
        intro_signature(project.settings),
    )
    saved = root / "render_plan.json"

    def finish_plan(plan: RenderPlan) -> RenderPlan:
        requested = max(1, round(total_target_seconds * plan.output_fps))
        if (plan.target_frame_count < requested
                and output_frame_count(project.settings, plan.output_fps,
                                       plan.actual_frame_count) > requested):
            raise ValueError(
                "Монтаж не укладывается в заданное время вместе с заставкой/презентацией. "
                "Увеличьте длительность или измените ручные ограничения скорости."
            )
        result = plan.model_copy(update={"settings": plan.settings | {
            "request_key": request_key, "duration_budget": "total-207",
            "target_seconds": total_target_seconds,
            "requested_output_frames": requested,
            "actual_output_frames": output_frame_count(
                project.settings, plan.output_fps, plan.actual_frame_count,
            ),
        }})
        validate_plan(project, result)
        atomic_write(saved, result.model_dump_json(indent=2))
        return result

    if saved.is_file():
        try:
            cached = RenderPlan.model_validate_json(saved.read_text(encoding="utf-8"))
            if cached.settings.get("request_key") == request_key:
                validate_plan(project, cached)
                return cached
        except (ValueError, OSError):
            pass  # A derived plan can be rebuilt from the validated project and analysis.
    if project.settings.preset == "speed" or project.settings.preset in PHOTOSHOP_PRESETS:
        if project.settings.preset in PHOTOSHOP_PRESETS:
            from sculptcut.photoshop_idle import project_photoshop_idle

            idle = project_photoshop_idle(
                root, project, cancel, (lambda value: progress(value * 0.35)) if progress else None
            )
            plan = exclude_dropped(project, build_plan(project, idle, target_seconds))
            if project.settings.preset == "photoshop_beat_shorts":
                from sculptcut.photoshop_beats import photoshop_beat_plan

                plan = photoshop_beat_plan(root, project, plan, cancel, progress)
                plan = apply_overrides(project, plan)
                plan = present_final_result(project, plan)
                return finish_plan(plan)
            from sculptcut.photoshop_stability import score_source_window
            from sculptcut.photoshop_story import episodic_photoshop_plan

            def score_drawing(source: SourceMedia, start: int, end: int) -> float:
                return score_source_window(
                    root, source, start, end, cancel, webcam_roi(project.settings, source.id),
                )

            drawing_progress = (lambda value: progress(0.35 + value * 0.2)) if progress else None
            if project.settings.preset == "photoshop_music_shorts":
                from sculptcut.music_episodes import music_episode_grid
                from sculptcut.photoshop_music import photoshop_music_plan

                phase("Подстраиваем смену видеоэпизодов под музыку")
                grid = music_episode_grid(root, project, plan.actual_frame_count, cancel)
                plan = photoshop_music_plan(
                    project, plan, grid, score_drawing, cancel, drawing_progress,
                )
            else:
                plan = episodic_photoshop_plan(project, plan, score_drawing, drawing_progress)
        else:
            plan = simple_speed_plan(project, target_seconds)
        if project.settings.preset in ("photoshop_shorts", "photoshop_music_shorts"):
            from sculptcut.photoshop import reframe_photoshop

            plan = apply_overrides(project, plan)
            if project.settings.preset == "photoshop_music_shorts":
                from sculptcut.music_episodes import sync_music_episodes

                plan = sync_music_episodes(root, project, plan, cancel)
            plan = plan.model_copy(
                update={
                    "settings": plan.settings
                    | {"width": 1080, "height": 1920, "tracking": "accelerated"}
                }
            )
            plan = reframe_photoshop(
                project,
                plan,
                cancel,
                (lambda value: progress(0.55 + value * 0.45)) if progress else None,
            )
            validate_plan(project, plan)
        plan = apply_square_layout(project, plan, root, cancel)
        if project.settings.preset != "photoshop_music_shorts":
            plan = present_final_result(project, plan)
        validate_plan(project, plan)
        return finish_plan(plan)
    connection = database(root)
    try:
        for source in project.sources:
            row = connection.execute(
                "SELECT position FROM checkpoints WHERE stage='features' AND source_id=? AND key=?",
                (source.id, analysis_key(project, source)),
            ).fetchone()
            if not row or row[0] < source.duration_us - 2_000_000 / project.settings.proxy_fps:
                raise ValueError("Analysis incomplete; run analyze before planning")
    finally:
        connection.close()
    plan = exclude_dropped(
        project, build_plan(project, project_idle(root, project), target_seconds),
    )
    from sculptcut.importance import importance_project

    phase("Analyzing camera motion and classifying activity")
    segments = importance_project(root, project, cancel)
    if project.settings.preset == "beat_shorts":
        from sculptcut.beat_shorts import beat_still_plan

        plan = beat_still_plan(root, project, plan, segments, target_seconds, cancel, progress)
        plan = apply_overrides(project, plan)
        from sculptcut.beat_sync import apply_beat_sync

        if plan.settings.get("beat_still_fallback") == "motion":
            plan = apply_beat_sync(root, project, plan, cancel)
        plan = apply_square_layout(project, plan, root, cancel)
        plan = present_final_result(project, plan)
        validate_plan(project, plan)
        return finish_plan(plan)
    if project.settings.preset in ("shorts", "music_shorts"):
        from sculptcut.process_story import process_plan

        plan = process_plan(root, project, plan, segments, cancel)
    elif (
        project.settings.preset == "timelapse"
        and project.settings.expressive_pacing
        and target_seconds >= 2 * project.settings.timelapse_episode_output_seconds
        and sum(d.source_end_us - d.source_start_us for d in plan.decisions if d.action != "DROP")
        >= 2 * project.settings.timelapse_episode_source_seconds * 1_000_000
    ):
        from sculptcut.shorts_quality import EpisodeInspector
        from sculptcut.timelapse_story import episodic_timelapse_plan

        phase("Selecting episodes by persistent visual changes")
        with EpisodeInspector(root, project, cancel) as inspector:
            plan = episodic_timelapse_plan(project, plan, segments, inspector.development)
        from sculptcut.timelapse_coverage import limit_setup_context, preserve_work_bridges

        plan = preserve_work_bridges(project, plan, segments)
        plan = limit_setup_context(project, plan, segments)
    elif (
        project.settings.expressive_pacing
        and sum(s.duration_us for s in project.sources) >= 30_000_000
    ):
        from sculptcut.pacing import paced_plan

        plan = paced_plan(project, plan, segments)
    else:
        plan = weighted_plan(project, plan, segments)
    if project.settings.preset not in ("shorts", "music_shorts"):
        from sculptcut.screen_cleanup import project_screens, strip_screens

        phase("Checking selected clips for blank screens")
        plan = strip_screens(project, plan, project_screens(root, project, cancel, plan, progress))
    if project.settings.preset == "timelapse":
        from sculptcut.timelapse_cleanup import clean_timelapse

        phase("Removing zoom-only movements and static views")
        plan = clean_timelapse(root, project, plan, cancel, progress)
    plan = apply_overrides(project, plan)
    if plan.settings.get("story") == "static_episodes":
        from sculptcut.timelapse_story import reserve_final_view

        plan = reserve_final_view(project, plan)
        from sculptcut.timelapse_coverage import balance_process_budget, limit_setup_context

        plan = limit_setup_context(project, plan, segments)
        plan = balance_process_budget(project, plan)
    from sculptcut.montage import apply_montage

    if plan.settings.get("story") not in ("process", "static_episodes"):
        plan = apply_montage(root, project, plan)
    from sculptcut.rhythm import align_plan

    if (plan.settings.get("story") != "static_episodes"
            and project.settings.preset != "music_shorts"):
        plan = align_plan(root, project, plan, cancel)
    from sculptcut.cropping import apply_crop

    if project.settings.preset in ("shorts", "music_shorts"):
        from sculptcut.shorts import reframe_shorts

        plan = reframe_shorts(
            root,
            project,
            plan,
            cancel,
            progress,
        )
        from sculptcut.beat_sync import apply_beat_sync

        plan = apply_beat_sync(root, project, plan, cancel)
    else:
        plan = apply_crop(root, project, plan, cancel)
    plan = apply_square_layout(project, plan, root, cancel)
    if project.settings.preset == "music_shorts":
        from sculptcut.music_episodes import sync_music_episodes

        phase("Подстраиваем смену видеоэпизодов под музыку")
        plan = sync_music_episodes(root, project, plan, cancel)
    else:
        plan = present_final_result(project, plan)
    validate_plan(project, plan)
    return finish_plan(plan)
