"""Conservative colour-background tracking of a drawing, without ZBrush assumptions."""

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import cv2
import numpy as np
from numpy.typing import NDArray

from sculptcut.domain import CameraPoint, Project, Roi, SubjectCamera
from sculptcut.media import Cancelled, MediaError
from sculptcut.planning import RenderPlan
from sculptcut.shorts import viewport_shorts_camera
from sculptcut.subject_camera import tracking_clip
from sculptcut.webcam import AnalysisCapture, webcam_roi


@dataclass(frozen=True)
class DrawingSettings:
    colour_bin: int = 16
    background_distance: float = 32
    minimum_canvas_fraction: float = 0.2
    minimum_subject_fraction: float = 0.005
    maximum_subject_fraction: float = 0.8
    significant_part_fraction: float = 0.2
    background_lightness_distance: float = 70
    background_chroma_distance: float = 18
    background_candidates: int = 8
    minimum_background_part: float = 0.12
    nearby_part_distance: float = 0.08
    tracking_window_seconds: float = 2.0
    outlier_window_seconds: float = 0.2
    outlier_distance: float = 0.06
    neutral_background_distance: float = 12
    maximum_canvas_edge_range: float = 0.08
    stationary_center_range: float = 0.02
    scale_lock_ratio: float = 1.2
    canvas_border_fraction: float = 0.005


SETTINGS = DrawingSettings()


def centered_camera(
    samples: list[tuple[int, Roi | None]], fps: int, width: int, height: int,
    margin: float, viewport: Roi,
) -> SubjectCamera:
    """Fit the whole episode once; drawing edits must not animate the camera."""
    boxes = [box for _, box in samples if box is not None]
    if boxes:
        left = min(box.x for box in boxes)
        top = min(box.y for box in boxes)
        right = max(box.x + box.width for box in boxes)
        bottom = max(box.y + box.height for box in boxes)
    else:
        left, top = viewport.x, viewport.y
        right, bottom = left + viewport.width, top + viewport.height
    aspect = (9 / 16) * height / width
    camera_height = max(bottom - top, (right - left) / aspect) * margin
    return SubjectCamera(
        viewport=viewport, fit_subject=True, width=camera_height * aspect,
        height=camera_height, background=32,
        points=[CameraPoint(time_us=samples[0][0], x=(left + right) / 2,
                            y=(top + bottom) / 2)],
    )


def centered_still_cameras(
    samples: list[tuple[int, Roi | None]], width: int, height: int,
    margin: float, viewport: Roi,
) -> dict[int, SubjectCamera]:
    """Lock nearby equivalent views; compensate large source zooms per still."""
    valid = [box for _, box in samples if box is not None]
    result: dict[int, SubjectCamera] = {}
    for timestamp, box in samples:
        if box is None and valid:
            box = min(
                ((abs(timestamp - t), b) for t, b in samples if b is not None),
                key=lambda item: item[0],
            )[1]
        neighbors = [
            (t, b) for t, b in samples
            if b is not None and box is not None
            and max(b.width / box.width, box.width / b.width,
                    b.height / box.height, box.height / b.height) <= SETTINGS.scale_lock_ratio
            and abs(b.x + b.width / 2 - box.x - box.width / 2) <= SETTINGS.stationary_center_range
            and abs(b.y + b.height / 2 - box.y - box.height / 2) <= SETTINGS.stationary_center_range
        ]
        if neighbors:
            bounds = np.median([[b.x, b.y, b.width, b.height] for _, b in neighbors], axis=0)
            box = Roi(x=float(bounds[0]), y=float(bounds[1]),
                      width=float(bounds[2]), height=float(bounds[3]))
        camera = centered_camera([(timestamp, box)], 1, width, height, margin, viewport)
        result[timestamp] = camera.model_copy(update={
            "points": [camera.points[0].model_copy(update={"time_us": 0})],
        })
    return result


def drawing_viewport(canvases: list[Roi], manual: Roi | None = None) -> Roi:
    """Keep Photoshop chrome outside the fitted drawing, despite canvas changes."""
    if manual is not None:
        return manual
    if not canvases:
        return Roi(x=0, y=0, width=1, height=1)
    edges = np.median([[b.x, b.y, b.x + b.width, b.y + b.height] for b in canvases], axis=0)
    left, top, right, bottom = edges
    return Roi(x=float(left), y=float(top), width=float(right-left), height=float(bottom-top))


def stable_camera(
    samples: list[tuple[int, Roi | None]],
    fps: int,
    width: int,
    height: int,
    margin: float,
) -> SubjectCamera:
    """Bridge detection gaps and reject isolated errors without chasing the viewport."""
    full = Roi(x=0, y=0, width=1, height=1)
    valid = [i for i, (_, box) in enumerate(samples) if box is not None]
    if not valid:
        return SubjectCamera(
            viewport=full,
            fit_subject=True,
            width=1,
            height=1,
            background=32,
            points=[CameraPoint(time_us=samples[0][0], x=0.5, y=0.5)],
        )
    raw = np.array(
        [
            [box.x + box.width / 2, box.y + box.height / 2, box.width, box.height]
            for _, box in samples
            if box is not None
        ]
    )
    positions = np.arange(len(samples))
    values = np.column_stack([np.interp(positions, valid, raw[:, i]) for i in range(4)])
    outlier_radius = max(1, round(fps * SETTINGS.outlier_window_seconds / 2))
    # A persistent pan/zoom survives the median. An isolated false detection does not.
    robust = values.copy()
    for i in range(len(values)):
        median = np.median(values[max(0, i - outlier_radius) : i + outlier_radius + 1], axis=0)
        if np.linalg.norm(values[i, :2] - median[:2]) > SETTINGS.outlier_distance:
            robust[i, :2] = median[:2]
        for axis in (2, 3):
            if abs(np.log(values[i, axis] / median[axis])) > np.log(1.5):
                robust[i, axis] = median[axis]
    radius = max(1, round(fps * SETTINGS.tracking_window_seconds / 2))
    smooth = robust.copy()
    for i in range(len(values)):
        lo, hi = max(0, i - radius), min(len(values), i + radius + 1)
        offsets = positions[lo:hi] - i
        design = np.column_stack([np.ones(hi - lo), offsets])
        # Local linear smoothing preserves constant motion, including both endpoints.
        fitted = np.linalg.lstsq(design, robust[lo:hi], rcond=None)[0][0]
        # Do not pull the fitted camera back towards every raw measurement.
        # That old clamp preserved detector jitter and amplified it in 9:16.
        smooth[i, :2] = np.clip(fitted[:2], 0, 1)
        smooth[i, 2:] = np.maximum(fitted[2:], 1e-6)
    for axis in (0, 1):
        if float(np.ptp(smooth[:, axis])) <= SETTINGS.stationary_center_range:
            smooth[:, axis] = np.median(smooth[:, axis])
    aspect = (9 / 16) * height / width
    camera_h = np.maximum(smooth[:, 3], smooth[:, 2] / aspect) * margin
    # Small changes usually come from the evolving drawing or imperfect
    # detection, not an intentional Photoshop zoom.  Lock them to one scale.
    # A large source zoom still gets compensated, but only after the long
    # smoothing window above has removed frame-to-frame pulses.
    if float(camera_h.max() / camera_h.min()) <= SETTINGS.scale_lock_ratio:
        camera_h[:] = camera_h.max()
    camera_w = camera_h * aspect
    return SubjectCamera(
        viewport=full,
        fit_subject=True,
        width=float(camera_w.max()),
        height=float(camera_h.max()),
        background=32,
        points=[
            CameraPoint(
                time_us=t,
                x=float(np.clip(smooth[i, 0], 0, 1)),
                y=float(np.clip(smooth[i, 1], 0, 1)),
                width=float(camera_w[i]),
                height=float(camera_h[i]),
            )
            for i, (t, _) in enumerate(samples)
        ],
    )


def locked_camera(
    samples: list[tuple[int, Roi | None]],
    width: int,
    height: int,
    margin: float,
    viewport: Roi | None = None,
) -> SubjectCamera:
    """Hold the canvas vertically and slide only inside its horizontal bounds."""
    full = Roi(x=0, y=0, width=1, height=1)
    viewport = viewport or full
    boxes = [box for _, box in samples if box is not None]
    if not boxes:
        return SubjectCamera(
            viewport=viewport,
            fit_subject=True,
            width=viewport.width,
            height=viewport.height,
            background=32,
            points=[
                CameraPoint(
                    time_us=samples[0][0],
                    x=viewport.x + viewport.width / 2,
                    y=viewport.y + viewport.height / 2,
                )
            ],
        )
    aspect = (9 / 16) * height / width
    return viewport_shorts_camera(
        viewport,
        boxes,
        aspect,
        samples[0][0],
        32,
        side_margin=0.04 * margin / 1.25,
    )


def drawing_box(frame: NDArray[np.uint8], previous: Roi | None = None) -> Roi | None:
    """Find foreground on a mostly plain canvas; ambiguity keeps the whole frame."""
    subject, _ = drawing_and_canvas_box(frame, previous)
    return subject


def drawing_and_canvas_box(
    frame: NDArray[np.uint8], previous: Roi | None = None
) -> tuple[Roi | None, Roi | None]:
    """Detect the drawing and the visible Photoshop canvas in one colour pass."""
    h, w = frame.shape[:2]
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    central = lab[h // 8 : h * 7 // 8, w // 8 : w * 7 // 8]
    quantized = central.astype(np.int32) // SETTINGS.colour_bin
    keys = quantized[:, :, 0] * 256 + quantized[:, :, 1] * 16 + quantized[:, :, 2]
    histogram = np.bincount(keys.ravel())
    # Photoshop's canvas may have a lightness gradient. A single colour bin
    # splits it into unrelated bands and can mistake a panel for the subject.
    probe = central[::8, ::8].astype(np.float32)
    choices = []
    for mode in np.argsort(histogram)[-SETTINGS.background_candidates :]:
        if not histogram[mode]:
            continue
        colour = np.median(central[keys == mode], axis=0)
        delta = probe - colour
        chroma = float(np.linalg.norm(colour[1:] - 128))
        lightness_distance = (
            SETTINGS.background_lightness_distance
            if chroma >= 10
            else SETTINGS.neutral_background_distance
        )
        matching = (np.abs(delta[:, :, 0]) <= lightness_distance) & (
            np.linalg.norm(delta[:, :, 1:], axis=2) <= SETTINGS.background_chroma_distance
        )
        edge = max(1, matching.shape[1] // 10)
        border_score = max(float(np.mean(matching[:, :edge])), float(np.mean(matching[:, -edge:])))
        background_score = (
            float(np.mean(matching)) * (0.25 + min(chroma / 12, 2))
            + border_score * 0.35
            + float(colour[0]) / 255 * 0.2
        )
        choices.append((background_score, colour))
    background = max(choices, key=lambda item: item[0])[1]
    delta = lab.astype(np.float32) - background
    lightness_distance = (
        SETTINGS.background_lightness_distance
        if np.linalg.norm(background[1:] - 128) >= 10
        else SETTINGS.neutral_background_distance
    )
    flat = (
        (np.abs(delta[:, :, 0]) <= lightness_distance)
        & (np.linalg.norm(delta[:, :, 1:], axis=2) <= SETTINGS.background_chroma_distance)
    ).astype(np.uint8)
    _, _, stats, _ = cv2.connectedComponentsWithStats(flat)
    if len(stats) < 2:
        return None, None
    largest = int(stats[1:, 4].max())
    regions = [s for s in stats[1:] if s[4] >= largest * SETTINGS.minimum_background_part]
    x, y = min(int(s[0]) for s in regions), min(int(s[1]) for s in regions)
    cw = max(int(s[0] + s[2]) for s in regions) - x
    ch = max(int(s[1] + s[3]) for s in regions) - y
    area = sum(int(s[4]) for s in regions)
    if area < w * h * SETTINGS.minimum_canvas_fraction:
        return None, None
    canvas = Roi(x=x / w, y=y / h, width=cw / w, height=ch / h)
    # A zoomed drawing can meet the screen edge, leaving background on one side only.
    # Its background bounding box must not exclude the drawing itself.
    if x > w * 0.25 and x + cw >= w * 0.97:
        cw += x
        x = 0
    elif x <= w * 0.03 and x + cw < w * 0.75:
        cw = w - x
    foreground = (1 - flat[y : y + ch, x : x + cw]).astype(np.uint8)
    # Resampling mixes the canvas edge with Photoshop chrome. A one-pixel
    # strip connected to the drawing must not turn its bounds into the entire
    # landscape canvas. Exclude only this narrow perimeter before grouping.
    edge = max(1, round(min(cw, ch) * SETTINGS.canvas_border_fraction))
    foreground[:edge] = foreground[-edge:] = 0
    foreground[:, :edge] = foreground[:, -edge:] = 0
    foreground = cv2.morphologyEx(foreground, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(foreground, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates: list[NDArray[np.int32]] = [
        c.astype(np.int32)
        for c in contours
        if cv2.contourArea(c) >= cw * ch * SETTINGS.minimum_subject_fraction
    ]
    if not candidates:
        return None, canvas

    def score(contour: NDArray[np.int32]) -> float:
        bx, by, bw, bh = cv2.boundingRect(contour)
        continuity = 1.0
        if previous is not None:
            delta = float(
                np.hypot(
                    (x + bx + bw / 2) / w - previous.x - previous.width / 2,
                    (y + by + bh / 2) / h - previous.y - previous.height / 2,
                )
            )
            track_radius = max(previous.width, previous.height, 0.1)
            continuity += 8 * float(np.exp(-((delta / track_radius) ** 2)))
        # Long thin strips along the canvas edge belong to panels/scrollbars.
        if (bw > cw * 0.8 and bh < ch * 0.12 and (by < 3 or by + bh > ch - 3)) or (
            bh > ch * 0.8 and bw < cw * 0.08 and (bx < 3 or bx + bw > cw - 3)
        ):
            return 0.0
        return float(cv2.contourArea(contour)) * continuity

    main = max(candidates, key=score)
    if score(main) <= 0 or cv2.contourArea(main) > cw * ch * SETTINGS.maximum_subject_fraction:
        return None, canvas
    mx, my, mw, mh = cv2.boundingRect(main)

    def nearby(contour: NDArray[np.int32]) -> bool:
        bx, by, bw, bh = cv2.boundingRect(contour)
        dx = max(0, mx - bx - bw, bx - mx - mw) / w
        dy = max(0, my - by - bh, by - my - mh) / h
        return float(np.hypot(dx, dy)) <= SETTINGS.nearby_part_distance

    parts = [
        c
        for c in candidates
        if score(c) > 0
        and nearby(c)
        and cv2.contourArea(c) >= cv2.contourArea(main) * SETTINGS.significant_part_fraction
    ]
    bx, by, bw, bh = cv2.boundingRect(np.concatenate(parts))
    if bx <= edge:
        bw += bx
        bx = 0
    if by <= edge:
        bh += by
        by = 0
    if bx + bw >= cw - edge:
        bw = cw - bx
    if by + bh >= ch - edge:
        bh = ch - by
    nx, ny = (x + bx) / w, (y + by) / h
    return Roi(x=nx, y=ny, width=min(1 - nx, bw / w), height=min(1 - ny, bh / h)), canvas


def episode_canvas(boxes: list[Roi], subjects: list[Roi], manual: Roi | None) -> Roi:
    """Fix one viewport for an episode; reject large changes in detected borders."""
    if manual is not None:
        return manual
    full = Roi(x=0, y=0, width=1, height=1)
    if not boxes:
        return full
    edges = np.array([[box.x, box.y, box.x + box.width, box.y + box.height] for box in boxes])
    if np.max(np.ptp(edges, axis=0)) > SETTINGS.maximum_canvas_edge_range:
        return full
    left, top, right, bottom = np.median(edges, axis=0)
    if subjects:
        left = min(left, *(box.x for box in subjects))
        top = min(top, *(box.y for box in subjects))
        right = max(right, *(box.x + box.width for box in subjects))
        bottom = max(bottom, *(box.y + box.height for box in subjects))
    return Roi(x=float(left), y=float(top), width=float(right - left), height=float(bottom - top))


def reframe_photoshop(
    project: Project,
    plan: RenderPlan,
    cancel: Event | None,
    progress: Callable[[float], None] | None = None,
) -> RenderPlan:
    sources = {s.id: s for s in project.sources}
    decisions = []
    warnings = list(plan.warnings)
    with tempfile.TemporaryDirectory(prefix="sculptcut-drawing-") as temporary:
        for index, decision in enumerate(plan.decisions):
            if cancel and cancel.is_set():
                raise Cancelled("Photoshop tracking cancelled")
            if not decision.output_frame_count:
                decisions.append(decision)
                continue
            clip = tracking_clip(
                sources[decision.source_id],
                decision.source_start_us,
                decision.source_end_us,
                Path(temporary) / "drawing.mp4",
                cancel,
                decision.output_frame_count,
                plan.output_fps,
            )
            reader = AnalysisCapture(clip, webcam_roi(project.settings, decision.source_id))
            samples: list[tuple[int, Roi | None]] = []
            canvases: list[Roi] = []
            previous = None
            fallback = False
            try:
                frame_index = 0
                while True:
                    if cancel and cancel.is_set():
                        raise Cancelled("Photoshop tracking cancelled")
                    ok, frame = reader.read()
                    if not ok:
                        break
                    if frame_index >= decision.output_frame_count:
                        break
                    box, canvas = drawing_and_canvas_box(frame.astype(np.uint8), previous)
                    if canvas is not None:
                        canvases.append(canvas)
                    if box is not None:
                        previous = box
                    fallback |= box is None
                    timestamp = decision.source_start_us + (
                        (decision.source_end_us - decision.source_start_us)
                        * frame_index
                        // decision.output_frame_count
                    )
                    samples.append((timestamp, box))
                    frame_index += 1
            finally:
                reader.release()
            if len(samples) != decision.output_frame_count:
                raise MediaError("Cannot decode Photoshop tracking clip")
            source = sources[decision.source_id]
            width, height = source.width, source.height
            if source.rotation % 180 == 90:
                width, height = height, width
            viewport = drawing_viewport(canvases, project.rois.get(decision.source_id))
            camera = centered_camera(
                samples, plan.output_fps, width, height,
                project.settings.shorts_tracking_margin, viewport,
            )
            decisions.append(decision.model_copy(update={"subject_camera": camera}))
            if fallback:
                warnings.append(
                    "Photoshop: detection gaps bridged; full frame only if no subject found: "
                    f"{decision.source_id}"
                )
            if progress:
                progress((index + 1) / len(plan.decisions))
    return plan.model_copy(update={"decisions": decisions, "warnings": warnings})
