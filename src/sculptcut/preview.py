"""Bounded sequential proxy preview: one reader per source, one output encoder."""

import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from threading import Event

import cv2
import numpy as np

from sculptcut.audio import add_music
from sculptcut.domain import Project, Roi
from sculptcut.intro import active_intro_image, output_frame_count, prepend_intro
from sculptcut.media import Cancelled, MediaError
from sculptcut.music_library import soundtrack
from sculptcut.planning import RenderPlan, validate_plan
from sculptcut.presentation import attach_presentation
from sculptcut.project import validate_sources
from sculptcut.proxy import proxy_path
from sculptcut.roi import pixel_bounds
from sculptcut.shorts_layout import SHORTS_PRESETS, uses_square
from sculptcut.subject_camera import camera_preview, still_frame, tracking_clip


def render_preview(
    root: Path,
    project: Project,
    plan: RenderPlan,
    destination: Path,
    cancel: Event | None = None,
    progress: Callable[[float], None] | None = None,
) -> Path:
    from sculptcut.rendering import chunk_decisions, normalize_timing, verify_output

    validate_sources(root, project)
    validate_plan(project, plan)
    output_frames = output_frame_count(project.settings, plan.output_fps, plan.actual_frame_count)
    music = soundtrack(root, project.settings, output_frames / plan.output_fps, cancel)
    destination = destination.resolve()
    if destination.exists() or destination.suffix.lower() != ".mp4":
        raise ValueError("Output must be a new .mp4 path")
    destination.parent.mkdir(parents=True, exist_ok=True)
    out_w, out_h = (
        (360, 640)
        if project.settings.preset in SHORTS_PRESETS
        else (640, 360)
    )
    sources = {s.id: s for s in project.sources}
    with tempfile.TemporaryDirectory(prefix="preview-", dir=destination.parent) as folder:
        temporary = Path(folder)
        raw = temporary / "sampled.mp4"
        writer = cv2.VideoWriter(
            str(raw), cv2.VideoWriter.fourcc(*"mp4v"), plan.output_fps, (out_w, out_h)
        )
        if not writer.isOpened():
            raise MediaError("Cannot open preview encoder")
        capture: cv2.VideoCapture | None = None
        source_id = ""
        reader_kind = ""
        completed = 0
        try:
            for decision in chunk_decisions(plan):
                if cancel and cancel.is_set():
                    raise Cancelled("Preview cancelled")
                if (source_id != decision.source_id or decision.subject_camera
                        or decision.action == "HOLD" or reader_kind != "proxy"):
                    if capture is not None:
                        capture.release()
                    source_id = decision.source_id
                    reader_kind = (
                        "still" if decision.action == "HOLD"
                        else "tracking" if decision.subject_camera else "proxy"
                    )
                    accelerated = bool(decision.subject_camera) and (
                        plan.settings.get("story") == "process"
                        or plan.settings.get("tracking") == "accelerated"
                    )
                    time_scale = (
                        (
                            (decision.source_end_us - decision.source_start_us)
                            / 1e6
                            / (decision.output_frame_count / plan.output_fps)
                        )
                        if accelerated
                        else 1.0
                    )
                    if decision.action == "HOLD":
                        image = still_frame(
                            sources[source_id], decision.source_start_us,
                            temporary / "still.png", cancel, 960,
                        )
                        current = cv2.imdecode(
                            np.frombuffer(image.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR,
                        )
                        if current is None:
                            raise MediaError("Cannot decode preview still")
                        capture = cv2.VideoCapture()
                        current_us = decision.source_start_us
                        following = None
                        next_us = None
                    else:
                        media_path = (
                            tracking_clip(
                                sources[source_id],
                                decision.source_start_us,
                                decision.source_end_us,
                                temporary / "tracking.mp4",
                                cancel,
                                output_frames=decision.output_frame_count if accelerated else None,
                                output_fps=plan.output_fps,
                            )
                            if decision.subject_camera
                            else proxy_path(root, project, sources[source_id])
                        )
                        capture = cv2.VideoCapture(str(media_path))
                        time_offset = decision.source_start_us if decision.subject_camera else 0
                        ok, current = capture.read()
                        if not ok:
                            raise MediaError("Cannot read proxy for preview; create proxies first")
                        current_us = time_offset + round(
                            capture.get(cv2.CAP_PROP_POS_MSEC) * 1000 * time_scale
                        )
                        ok, following = capture.read()
                        next_us = (
                            time_offset + round(
                                capture.get(cv2.CAP_PROP_POS_MSEC) * 1000 * time_scale
                            )
                            if ok
                            else None
                        )
                assert capture is not None
                for index in range(decision.output_frame_count):
                    if cancel and cancel.is_set():
                        raise Cancelled("Preview cancelled")
                    target = decision.source_start_us
                    if decision.action != "HOLD":
                        target += (
                            (decision.source_end_us - target) * index // decision.output_frame_count
                        )
                    while next_us is not None and next_us <= target:
                        if cancel and cancel.is_set():
                            raise Cancelled("Preview cancelled")
                        if following is None:
                            raise MediaError("Preview next frame missing")
                        current = following
                        current_us = next_us
                        ok, following = capture.read()
                        new_us = (
                            time_offset
                            + round(capture.get(cv2.CAP_PROP_POS_MSEC) * 1000 * time_scale)
                            if ok
                            else None
                        )
                        if new_us is not None and new_us <= next_us:
                            raise MediaError("Proxy presentation timestamp mismatch")
                        next_us = new_us
                    if next_us is None and target - current_us > max(
                        2_000_000 / project.settings.proxy_fps,
                        2_000_000 / plan.output_fps * time_scale,
                    ):
                        raise MediaError("Proxy ended before requested preview time")
                    if current is None:
                        raise MediaError("Preview frame missing")
                    frame = current
                    if decision.subject_camera:
                        tracked_frame = camera_preview(
                                frame.astype(np.uint8),
                                decision.subject_camera,
                                current_us,
                                out_w,
                                out_w if uses_square(project.settings) else out_h,
                            )
                        if uses_square(project.settings):
                            canvas = np.full((out_h, out_w, 3), 24, dtype=np.uint8)
                            top = (out_h - out_w) // 2
                            canvas[top:top+out_w] = tracked_frame
                            tracked_frame = canvas
                        writer.write(tracked_frame)
                        completed += 1
                        if progress and completed % plan.output_fps == 0:
                            progress(completed / plan.actual_frame_count * 0.85)
                        continue
                    if isinstance(decision.crop, Roi):
                        x, y, right, bottom = pixel_bounds(
                            decision.crop, frame.shape[1], frame.shape[0]
                        )
                        frame = frame[y:bottom, x:right]
                    height, width = frame.shape[:2]
                    if uses_square(project.settings):
                        if not isinstance(decision.crop, Roi):
                            raise ValueError("Square Shorts requires a planned source crop")
                        side = min(out_w, out_h)
                        square_frame = cv2.resize(frame, (side, side))
                        if (
                            decision.action == "HOLD"
                            and project.settings.preset == "beat_shorts"
                            and decision.output_frame_count / plan.output_fps
                            >= float(plan.settings.get("quiet_hold_zoom_min_seconds", 0.7))
                            and decision.output_frame_count > 1
                        ):
                            zoom = 1 + (float(plan.settings.get("quiet_hold_zoom_max", 1)) - 1) * (
                                index + 1
                            ) / (decision.output_frame_count - 1)
                            zoom = min(zoom, float(plan.settings.get("quiet_hold_zoom_max", 1)))
                            extent = max(1, round(side / zoom))
                            offset = (side - extent) // 2
                            square_frame = cv2.resize(
                                square_frame[offset:offset+extent, offset:offset+extent],
                                (side, side),
                            )
                        canvas = np.full((out_h, out_w, 3), 24, dtype=np.uint8)
                        top, left = (out_h - side) // 2, (out_w - side) // 2
                        canvas[top:top+side, left:left+side] = square_frame
                        writer.write(canvas)
                        completed += 1
                        if progress and completed % plan.output_fps == 0:
                            progress(completed / plan.actual_frame_count * 0.85)
                        continue
                    ratio = min(out_w / width, out_h / height)
                    resized = cv2.resize(
                        frame, (max(1, round(width * ratio)), max(1, round(height * ratio)))
                    )
                    canvas = np.zeros((out_h, out_w, 3), dtype=np.uint8)
                    if project.settings.preset in SHORTS_PRESETS:
                        fill_ratio = max(out_w / width, out_h / height)
                        background = cv2.resize(
                            frame,
                            (
                                max(out_w, round(width * fill_ratio)),
                                max(out_h, round(height * fill_ratio)),
                            ),
                        )
                        bh, bw = background.shape[:2]
                        background = background[
                            (bh - out_h) // 2 : (bh - out_h) // 2 + out_h,
                            (bw - out_w) // 2 : (bw - out_w) // 2 + out_w,
                        ]
                        np.copyto(canvas, cv2.blur(background, (31, 31)))
                    h, w = resized.shape[:2]
                    canvas[
                        (out_h - h) // 2 : (out_h - h) // 2 + h,
                        (out_w - w) // 2 : (out_w - w) // 2 + w,
                    ] = resized
                    writer.write(canvas)
                    completed += 1
                    if progress and completed % plan.output_fps == 0:
                        progress(completed / plan.actual_frame_count * 0.85)
        finally:
            writer.release()
            if capture is not None:
                capture.release()
        ready = temporary / "preview.mp4"
        normalize_timing(
            raw,
            ready,
            plan.output_fps,
            cancel,
            screen_guard=plan.settings.get("story") == "process",
            bookend_plan=plan,
            square_bands=uses_square(project.settings),
            encoder="libx264",
        )
        if (intro_image := active_intro_image(project.settings)) is not None:
            intro = temporary / "intro.mp4"
            prepend_intro(
                ready,
                intro_image,
                intro,
                width=out_w,
                height=out_h,
                fps=plan.output_fps,
                body_frames=plan.actual_frame_count,
                settings=project.settings,
                encoder="libx264",
                cancel=cancel,
            )
            ready = intro
        if project.settings.presentation_video is not None:
            presentation = temporary / "presentation.mp4"
            attach_presentation(ready, presentation, project.settings, width=out_w,
                                height=out_h, fps=plan.output_fps,
                                encoder="libx264", cancel=cancel)
            ready = presentation
        if music:
            mixed = temporary / "music.mp4"
            add_music(
                ready,
                music,
                mixed,
                output_frames / plan.output_fps,
                project.settings.music_loop,
                project.settings.music_fade_seconds,
                cancel,
            )
            ready = mixed
        verify_output(
            ready,
            output_frames,
            plan.output_fps,
            cancel,
            expected_audio=bool(music),
        )
        validate_sources(root, project)
        validate_plan(project, plan)
        if destination.exists():
            raise ValueError("Output appeared during render; refusing overwrite")
        os.rename(ready, destination)
        if progress:
            progress(1)
    return destination
