"""Conservative colour-background tracking of a drawing, without ZBrush assumptions."""

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from threading import Event

import cv2
import numpy as np
from numpy.typing import NDArray

from sculptcut.canvas_edges import dark_document_bounds, trim_horizontal_chrome
from sculptcut.canvas_history import enabled as keep_whole_canvas
from sculptcut.domain import CameraPoint, Project, Roi, SubjectCamera
from sculptcut.drawing_page import SETTINGS as PAGE
from sculptcut.drawing_page import enclosed_page, lock_page_points, page_colour, page_subject
from sculptcut.drawing_reference import SETTINGS as REFERENCE
from sculptcut.drawing_reference import DrawingReference, locked_reference_points
from sculptcut.media import Cancelled, MediaError
from sculptcut.photoshop_closeup import (
    add_closeups,
    horizontal_focus,
    red_mask_candidate,
    transient_masks,
)
from sculptcut.planning import RenderPlan
from sculptcut.shorts import viewport_shorts_camera
from sculptcut.subject_camera import read_still, still_frame, tracking_clip
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
    motion_lock_center_range: float = 0.003
    sketch_reference_weight: float = .5


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
    stationary_range: float | None = None,
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
        if float(np.ptp(smooth[:, axis])) <= (
            SETTINGS.stationary_center_range if stationary_range is None else stationary_range
        ):
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


def shared_scene_scales(cameras: list[SubjectCamera]) -> list[SubjectCamera]:
    """Share one fit across comparable episodes, without merging genuine source zooms."""
    result = list(cameras)
    static = sorted((i for i, c in enumerate(cameras)
                     if all(abs((p.height or c.height)-c.height) < 1e-9
                            for p in c.points)), key=lambda i: cameras[i].height, reverse=True)
    start = 0
    while start < len(static):
        anchor = cameras[static[start]]
        end = start + 1
        while (end < len(static)
               and anchor.height/cameras[static[end]].height <= SETTINGS.scale_lock_ratio):
            end += 1
        for i in static[start:end]:
            result[i] = cameras[i].model_copy(update={
                'width': anchor.width, 'height': anchor.height,
                'points': [p.model_copy(update={'width': None, 'height': None})
                           for p in cameras[i].points],
            })
            points = result[i].points
            if all(p.x == points[0].x and p.y == points[0].y for p in points):
                result[i] = result[i].model_copy(update={'points': points[:1]})
        start = end
    return result


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
    x, y, right, bottom = trim_horizontal_chrome(frame, (x, y, x+cw, y+ch))
    cw, ch = right-x, bottom-y
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
    plan = plan.model_copy(update={'settings':{k:v for k,v in plan.settings.items()
                                               if k != 'photoshop_frame_only'}})
    sources = {s.id: s for s in project.sources}
    decisions = []
    warnings = [w for w in plan.warnings if not w.startswith("Photoshop:")]
    references: dict[str, tuple[float, NDArray[np.uint8], Roi, Roi]] = {}
    episode_references: dict[int, tuple[float, int, Roi, Roi]] = {}
    ordinary = (project.settings.preset == 'photoshop_shorts'
                and not project.settings.photoshop_keep_model
                and project.settings.shorts_layout == 'auto')
    clips: dict[int, tuple[Path, list[int], Roi]] = {}
    measured_boxes: dict[int, list[Roi | None]] = {}
    page_samples: dict[int, list[Roi | None]] = {}
    canvas_samples: dict[int, list[Roi | None]] = {}
    closeup_views: dict[int, list[Roi | None]] = {}
    closeup_focuses: dict[int, list[float | None]] = {}
    mask_samples: dict[str, list[tuple[int, int, bool | None]]] = {}
    dark_workspace_shots: set[int] = set()
    dark_workspace_sources: set[str] = set()
    sketch_samples: dict[int, list[bool]] = {}
    page_background = None
    whole_canvas = keep_whole_canvas(project.settings)
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
                Path(temporary) / f"drawing-{index}.mp4",
                cancel,
                decision.output_frame_count,
                plan.output_fps,
                rgb=True,
            )
            reader = AnalysisCapture(clip, webcam_roi(project.settings, decision.source_id))
            samples: list[tuple[int, Roi | None]] = []
            canvases: list[Roi] = []
            document_views: dict[int, Roi] = {}
            manual = project.rois.get(decision.source_id)
            previous = None
            fallback = False
            pages: list[Roi | None] = []
            visible_canvases: list[Roi | None] = []
            safe_views: list[Roi | None] = []
            focuses: list[float | None] = []
            sketches: list[bool] = []
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
                    document = (dark_document_bounds(frame)
                                if whole_canvas and manual is None else None)
                    if document is None:
                        box, canvas = drawing_and_canvas_box(frame, previous)
                    else:
                        x, y, doc_right, doc_bottom = document
                        h, w = frame.shape[:2]
                        cw, ch = doc_right-x, doc_bottom-y
                        canvas = Roi(x=x/w, y=y/h, width=cw/w, height=ch/h)
                        document_views[frame_index] = canvas
                        box, _ = drawing_and_canvas_box(frame[y:doc_bottom, x:doc_right])
                        if box is not None:
                            box = Roi(x=(x+box.x*cw)/w, y=(y+box.y*ch)/h,
                                      width=box.width*cw/w, height=box.height*ch/h)
                    page = enclosed_page(frame.astype(np.uint8),rotated=ordinary)
                    sketch = False
                    if page is not None and (box is None or box.width*box.height >= (
                        page.width*page.height*PAGE.maximum_subject_coverage
                    )):
                        sketch_box = page_subject(frame.astype(np.uint8), page)
                        if sketch_box is not None:
                            box, canvas, sketch = sketch_box, page, True
                    pages.append(page)
                    visible_canvases.append(canvas)
                    sketches.append(sketch)
                    red_mask = document is not None and red_mask_candidate(frame, document)
                    if whole_canvas:
                        safe_view = ((document_views.get(frame_index) or page)
                                     if manual is None and not red_mask else None)
                        safe_views.append(safe_view)
                        focuses.append(horizontal_focus(frame, box, safe_view))
                        mask_samples.setdefault(decision.source_id, []).append((
                            index, frame_index, red_mask if document is not None else None))
                    if page is not None and page_background is None:
                        page_background = page_colour(frame.astype(np.uint8), page)
                    if canvas is not None:
                        canvases.append(canvas)
                    if box is not None:
                        previous = box
                    view = manual or canvas
                    if box is not None and view is not None and not red_mask:
                        edge = REFERENCE.reference_edge_margin
                        edges = sum((box.x <= view.x+edge, box.y <= view.y+edge,
                            box.x+box.width >= view.x+view.width-edge,
                            box.y+box.height >= view.y+view.height-edge))
                        quality = (-REFERENCE.clipped_edge_penalty*edges
                                   + box.width*box.height/(view.width*view.height))
                        if sketch:
                            quality *= SETTINGS.sketch_reference_weight
                        best = references.get(decision.source_id)
                        if best is None or quality > best[0]:
                            references[decision.source_id] = (quality, frame.copy(), box, view)
                        if ordinary:
                            local_best = episode_references.get(index)
                            if local_best is None or quality > local_best[0]:
                                episode_references[index] = (quality, frame_index, box, view)
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
            viewport = drawing_viewport(canvases, manual)
            if document_views:
                # Decode the union, but require a fresh document detection for
                # each pixel update once this workspace is confirmed.
                dark_workspace_shots.add(index)
                dark_workspace_sources.add(decision.source_id)
                confirmed = document_views.values()
                left = min(view.x for view in confirmed)
                top = min(view.y for view in confirmed)
                right = max(view.x+view.width for view in confirmed)
                bottom = max(view.y+view.height for view in confirmed)
                viewport = Roi(x=left, y=top, width=right-left, height=bottom-top)
                visible_canvases = [document_views.get(i) for i in range(len(samples))]
            camera = stable_camera(
                samples, plan.output_fps, width, height,
                project.settings.shorts_tracking_margin, SETTINGS.motion_lock_center_range,
            ).model_copy(update={'viewport': viewport})
            decisions.append(decision.model_copy(update={"subject_camera": camera}))
            clips[index] = (clip, [t for t, _ in samples], viewport)
            measured_boxes[index] = [box for _, box in samples]
            page_samples[index] = pages
            canvas_samples[index] = visible_canvases
            closeup_views[index] = safe_views
            closeup_focuses[index] = focuses
            sketch_samples[index] = sketches
            if fallback:
                warnings.append(
                    "Photoshop: detection gaps bridged; full frame only if no subject found: "
                    f"{decision.source_id}"
                )
            if progress:
                progress(.5*(index + 1) / len(plan.decisions))
        for samples_by_source in mask_samples.values():
            for shot, frame_index in transient_masks(samples_by_source, plan.output_fps):
                canvas_samples[shot][frame_index] = None
                page_samples[shot][frame_index] = None
                closeup_views[shot][frame_index] = None
        # All selected episodes are fitted to the most complete view, rather than
        # centring a leg/detail crop as if it were the whole drawing.
        total_samples = sum(len(samples) for samples in page_samples.values())
        valid_pages = sum(p is not None for samples in page_samples.values() for p in samples)
        subjects = sum(box is not None and (page is None or
            box.width*box.height < page.width*page.height*PAGE.maximum_subject_coverage)
            for i, boxes in measured_boxes.items()
            for box, page in zip(boxes, page_samples[i], strict=True))
        if ordinary:
            from sculptcut.photoshop_local import local_shots

            local = local_shots(project,decisions,clips,episode_references,page_samples,
                                canvas_samples,cancel,progress,prefer_pages=bool(total_samples
                                    and valid_pages >= total_samples*PAGE.minimum_tracking_fraction
                                    and subjects <= total_samples*PAGE.maximum_subject_fraction))
            return plan.model_copy(update={'decisions':local,'settings':plan.settings | {
                'photoshop_camera':'local-episode-366','photoshop_frame_only':1,
                'photoshop_registered_shots':len(clips)},'warnings':warnings})
        if (project.settings.photoshop_keep_model and not project.rois and total_samples
                and valid_pages >= total_samples*PAGE.minimum_tracking_fraction
                and subjects <= total_samples*PAGE.maximum_subject_fraction):
            aspects: list[float] = []
            for i, values in page_samples.items():
                source = sources[decisions[i].source_id]
                sw, sh = source.width, source.height
                if source.rotation % 180 == 90:
                    sw, sh = sh, sw
                aspects.extend(page.width*sw/(page.height*sh)
                               for page in values if page is not None)
            reference_aspect = float(np.median(aspects))
            for index, pages in page_samples.items():
                source = sources[decisions[index].source_id]
                sw, sh = source.width, source.height
                if source.rotation % 180 == 90:
                    sw, sh = sh, sw
                times = clips[index][1]
                decision = decisions[index]
                if decision.action == 'HOLD':
                    timestamp = decision.source_start_us
                    if decision.provenance in ('user:final_tail', 'user:shorts_final'):
                        timestamp = max(timestamp, decision.source_end_us-ceil(
                            1_000_000*source.fps_den/source.fps_num))
                    image_path = still_frame(source, timestamp,
                        Path(temporary)/f'page-hold-{index}.png', cancel, sample_width=960)
                    page = enclosed_page(read_still(image_path))
                    pages = [page]*len(times)
                poses = []
                views = []
                for t, page in zip(times, pages, strict=True):
                    if page is None:
                        continue
                    aspect = page.width*sw/(page.height*sh)
                    if max(aspect/reference_aspect, reference_aspect/aspect) > (
                        PAGE.maximum_aspect_ratio_change
                    ):
                        continue
                    camera = centered_camera([(t, page)], 1, sw, sh,
                        PAGE.framing_margin, page)
                    poses.append(camera.points[0].model_copy(update={
                        'width':camera.width, 'height':camera.height, 'reference_matched':True,
                        'visible_region':page}))
                    views.append(page)
                if poses:
                    # Decode the union; only each point's actual visible page is
                    # copied to the canvas, including when the source pans/zooms.
                    left = min(v.x for v in views)
                    top = min(v.y for v in views)
                    right = max(v.x+v.width for v in views)
                    bottom = max(v.y+v.height for v in views)
                    viewport = Roi(x=left, y=top, width=right-left, height=bottom-top)
                    trusted = {p.time_us:p for p in poses}
                    poses = [min(poses, key=lambda p: abs(p.time_us-t)).model_copy(update={
                        'time_us':t, 'reference_matched':t in trusted}) for t in times]
                    decisions[index] = decisions[index].model_copy(update={'subject_camera':
                        camera.model_copy(update={'viewport':viewport, 'canvas_registered':True,
                            'points':poses, 'background_rgb':page_background})})
                else:
                    old_camera = decisions[index].subject_camera
                    assert old_camera is not None
                    decisions[index] = decisions[index].model_copy(update={'subject_camera':
                        old_camera.model_copy(update={'canvas_registered':False})})
            for sid in sources:
                page_cameras = {i:c for i in page_samples if decisions[i].source_id == sid
                                and (c := decisions[i].subject_camera) is not None
                                and c.canvas_registered}
                page_poses = [point for c in page_cameras.values() for point in c.points]
                locked = iter(lock_page_points(page_poses))
                for i, page_camera in page_cameras.items():
                    decisions[i] = decisions[i].model_copy(update={'subject_camera':
                        page_camera.model_copy(update={'points':[
                            next(locked) for _ in page_camera.points]})})
            closeups = add_closeups(project, decisions, plan.output_fps, clips,
                                   measured_boxes, closeup_views, closeup_focuses, cancel,
                                   (lambda value: progress(.5+.5*value)) if progress else None)
            return plan.model_copy(update={'decisions':decisions, 'settings':plan.settings | {
                'photoshop_camera':'enclosed-page-361', 'photoshop_registered_shots':sum(
                    bool(d.output_frame_count and d.subject_camera
                         and d.subject_camera.canvas_registered) for d in decisions),
                'photoshop_reference_source':'page', 'photoshop_closeup_shots':closeups},
                'warnings':[w for w in warnings if not w.startswith('Photoshop:')]})
        registrations = {}
        # One reference for the whole montage, not a new frame for each input file.
        best_reference = max(references, key=lambda sid: references[sid][0], default=None)
        for sid in ([best_reference] if best_reference is not None else []):
            _, image, box, viewport = references[sid]
            source = sources[sid]
            sw, sh = source.width, source.height
            if source.rotation % 180 == 90:
                sw, sh = sh, sw
            reference_camera = centered_camera([(0, box)], 1, sw, sh,
                project.settings.shorts_tracking_margin, viewport)
            registrations[sid] = DrawingReference(image, viewport, reference_camera)
        reference = registrations.get(best_reference) if best_reference is not None else None
        reference_box = references[best_reference][2] if best_reference is not None else None
        registered: set[int] = set()
        source_references: dict[str, list[DrawingReference]] = {}
        for number, (index, (clip, times, viewport)) in enumerate(clips.items()):
            registration = reference
            source_id = decisions[index].source_id
            manual = project.rois.get(source_id)
            anchors = source_references.get(source_id, [])
            require_page = source_id in dark_workspace_sources and index not in dark_workspace_shots
            anchor_image = None
            anchor_point = None
            anchor_view = viewport
            points: list[CameraPoint | None] = []
            if registration is not None:
                reader = AnalysisCapture(clip, webcam_roi(
                    project.settings, decisions[index].source_id))
                try:
                    for t in times:
                        if cancel and cancel.is_set():
                            raise Cancelled('Photoshop reference fitting cancelled')
                        ok, frame = reader.read()
                        if not ok:
                            raise MediaError('Cannot decode Photoshop reference clip')
                        # Follow the last trusted view within this source first. Artwork
                        # changes between recordings must not rematch a face to a repeated
                        # detail of a distant reference and suddenly halve the scale.
                        point = None
                        score = 0.0
                        visible = (page_samples if require_page else canvas_samples)[index][
                            len(points)]
                        if whole_canvas and visible is None:
                            points.append(None)
                            continue
                        match_view = (manual or visible or viewport) if whole_canvas else viewport
                        features = registration.features(frame, match_view)
                        for candidate in [*anchors, registration]:
                            matched = candidate.point(frame, match_view, t, features)
                            if matched is not None and candidate.last_score > score:
                                point, score = matched, candidate.last_score
                        if point is not None:
                            anchor_image, anchor_point = frame.copy(), point
                            anchor_view = match_view
                        # Textureless complete drawings can still provide a reliable bbox.
                        # Never use a clipped detail/head as a replacement full-body reference.
                        box = measured_boxes[index][len(points)]
                        if (point is None and box is not None and reference_box is not None
                                and (len(registration.keys) < REFERENCE.minimum_inliers
                                     or sketch_samples[index][len(points)])):
                            fh, fw = frame.shape[:2]
                            aspect = box.width*fw/(box.height*fh)
                            ref_aspect = (reference_box.width*registration.width
                                          / (reference_box.height*registration.height))
                            edge = REFERENCE.reference_edge_margin
                            complete = (box.x > viewport.x+edge and box.y > viewport.y+edge
                                and box.x+box.width < viewport.x+viewport.width-edge
                                and box.y+box.height < viewport.y+viewport.height-edge)
                            if complete and max(aspect/ref_aspect, ref_aspect/aspect) <= (
                                REFERENCE.maximum_whole_subject_aspect_ratio
                            ):
                                tracking = decisions[index].subject_camera
                                assert tracking is not None
                                pose = tracking.points[min(len(points),len(tracking.points)-1)]
                                point = pose.model_copy(update={'time_us':t,
                                    'width':pose.width or tracking.width,
                                    'height':pose.height or tracking.height})
                        if point is not None and visible is not None:
                            left, top = max(visible.x, viewport.x), max(visible.y, viewport.y)
                            right = min(visible.x+visible.width, viewport.x+viewport.width)
                            bottom = min(visible.y+visible.height, viewport.y+viewport.height)
                            if right > left and bottom > top:
                                point = point.model_copy(update={'visible_region':Roi(
                                    x=left, y=top, width=right-left, height=bottom-top)})
                            else:
                                point = None
                        else:
                            point = None
                        points.append(point)
                finally:
                    reader.release()
            valid = [i for i, p in enumerate(points) if p is not None]
            if valid and len(valid) >= len(times)*REFERENCE.minimum_tracking_fraction:
                candidate_camera = decisions[index].subject_camera
                assert candidate_camera is not None
                assert registration is not None
                camera = candidate_camera.model_copy(update={
                    'canvas_registered': True,
                    'background_rgb': registration.camera.background_rgb,
                    'points': [p.model_copy(update={
                        'reference_matched': raw is not None,
                        'visible_region':raw.visible_region if raw else None})
                               for p, raw in zip(locked_reference_points(points, times),
                                                 points, strict=True)]})
                decisions[index] = decisions[index].model_copy(update={'subject_camera':camera})
                registered.add(index)
                if anchor_image is not None and anchor_point is not None:
                    bank = source_references.setdefault(decisions[index].source_id, [])
                    assert anchor_point.height is not None and anchor_point.width is not None
                    diverse = all(
                        max(anchor_point.height/a.camera.height,
                            a.camera.height/anchor_point.height) > REFERENCE.anchor_scale_ratio
                        or abs(anchor_point.x-a.camera.points[0].x)/anchor_point.width
                            > REFERENCE.anchor_translation_fraction
                        or abs(anchor_point.y-a.camera.points[0].y)/anchor_point.height
                            > REFERENCE.anchor_translation_fraction for a in bank)
                    if diverse and len(bank) < REFERENCE.source_anchors:
                        bank.append(DrawingReference(anchor_image, anchor_view, camera.model_copy(
                            update={'width':anchor_point.width, 'height':anchor_point.height,
                                    'points':[anchor_point]})))
            else:
                warnings.append('Photoshop: no reliable reference match; fixed framing retained: '
                                + decisions[index].source_id)
            if progress:
                fraction = .4 if whole_canvas else .5
                progress(.5+fraction*(number+1)/max(1,len(clips)))
        for i, decision in enumerate(decisions):
            if not decision.output_frame_count or i in registered:
                continue
            neighbors = [j for j in registered if decisions[j].source_id == decision.source_id]
            if neighbors:
                j = min(neighbors, key=lambda j: abs(
                    decisions[j].source_start_us-decision.source_start_us))
                fallback_camera = decisions[j].subject_camera
            else:
                fallback_camera = (reference.camera if reference is not None
                                   else decision.subject_camera)
            assert fallback_camera is not None and decision.subject_camera is not None
            point = fallback_camera.points[0].model_copy(
                update={'time_us':decision.source_start_us})
            decisions[i] = decision.model_copy(update={'subject_camera':fallback_camera.model_copy(
                update={'points':[point], 'viewport':decision.subject_camera.viewport,
                        'canvas_registered':False})})
        closeups = (add_closeups(project, decisions, plan.output_fps, clips,
                                measured_boxes, closeup_views, closeup_focuses, cancel,
                                (lambda value: progress(.9+.1*value)) if progress else None)
                    if whole_canvas else 0)
    return plan.model_copy(update={"decisions": decisions, "warnings": warnings,
        'settings': plan.settings | {'photoshop_camera': 'fixed-reference-352',
                                    'photoshop_reference_source': best_reference or '',
                                    'photoshop_registered_shots':len(registered),
                                    'photoshop_closeup_shots':closeups}})
