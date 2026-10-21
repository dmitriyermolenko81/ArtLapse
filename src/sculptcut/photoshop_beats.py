"""Musical drawing milestones with one immobile camera per source."""

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import cv2
import numpy as np
from numpy.typing import NDArray

from sculptcut.beat_shorts import beat_boundaries
from sculptcut.domain import CameraPoint, Project, Roi, SourceMedia, SubjectCamera
from sculptcut.media import Cancelled, MediaError
from sculptcut.photoshop import SETTINGS as DRAWING_SETTINGS
from sculptcut.photoshop import centered_still_cameras, drawing_and_canvas_box, drawing_viewport
from sculptcut.photoshop_stability import score_source_window
from sculptcut.planning import RenderDecision, RenderPlan, validate_plan
from sculptcut.roi import pixel_bounds
from sculptcut.shorts_layout import apply_square_layout, uses_square
from sculptcut.square_tracking import safe_viewport
from sculptcut.subject_camera import still_frame
from sculptcut.webcam import webcam_roi, without_webcam


@dataclass(frozen=True)
class PhotoshopBeatSettings:
    candidates_per_slot: int = 3
    stability_window_us: int = 1_000_000
    sample_width: int = 640


SETTINGS = PhotoshopBeatSettings()


def photoshop_beat_plan(
    root: Path,
    project: Project,
    base: RenderPlan,
    cancel: Event | None = None,
    progress: Callable[[float], None] | None = None,
) -> RenderPlan:
    """Select chronological stable views from retained work, on the music grid."""
    boundaries = beat_boundaries(root, project, base.target_frame_count, cancel)
    slots = len(boundaries) - 1
    sources = {source.id: source for source in project.sources}
    spans: list[tuple[int, int, RenderDecision]] = []
    total = 0
    for decision in base.decisions:
        if decision.action != "DROP":
            end = total + decision.source_end_us - decision.source_start_us
            spans.append((total, end, decision))
            total = end
    if not spans:
        raise ValueError("Photoshop: no retained work for music montage")

    chosen: list[tuple[str, int, int]] = []
    samples: dict[str, list[tuple[int, Roi | None]]] = {}
    canvases: dict[str, list[Roi]] = {}
    backdrops: dict[tuple[str, int], tuple[Roi | None, tuple[int, int, int]]] = {}
    with tempfile.TemporaryDirectory(prefix="photoshop-beats-", dir=root) as directory:
        frame_path = Path(directory) / "sample.png"
        for slot in range(slots):
            if cancel and cancel.is_set():
                raise Cancelled("Photoshop music planning cancelled")
            options: list[tuple[float, int, str, int]] = []
            left, right = total * slot // slots, total * (slot + 1) // slots
            for candidate in range(SETTINGS.candidates_per_slot):
                position = left + (right - left) * (2 * candidate + 1) // (
                    2 * SETTINGS.candidates_per_slot
                )
                if slot == slots - 1:
                    position = total - 1
                start, end, span = next(
                    (a, b, decision) for a, b, decision in spans if a <= position < b
                )
                window = min(SETTINGS.stability_window_us, end - start)
                window_start = span.source_start_us + max(
                    0, min(position - start - window // 2, end - start - window)
                )
                source = sources[span.source_id]
                frame_us = max(1, round(1_000_000 * source.fps_den / max(1, source.fps_num)))
                timestamp = min(
                    span.source_start_us + position - start,
                    max(span.source_start_us, span.source_end_us - frame_us),
                )
                score = score_source_window(
                    root, source, window_start, window_start + window, cancel,
                    webcam_roi(project.settings, source.id),
                )
                options.append((score, abs(position - (left + right) // 2), source.id, timestamp))
                if slot == slots - 1:
                    break
            _, _, source_id, timestamp = min(options)
            # Short partitions can share the same stability window. Keep source
            # intervals strictly ordered without inventing extra source footage.
            if chosen and chosen[-1][0] == source_id:
                timestamp = max(timestamp, chosen[-1][1] + 1)
            source = sources[source_id]
            frame_us = max(1, round(1_000_000 * source.fps_den / max(1, source.fps_num)))
            timestamp = min(timestamp, max(0, source.duration_us - frame_us))
            if chosen and chosen[-1][:2] == (source_id, timestamp):
                previous_id, previous_time, previous_count = chosen[-1]
                chosen[-1] = (
                    previous_id, previous_time,
                    previous_count + boundaries[slot + 1] - boundaries[slot],
                )
                continue
            box, canvas = _measure_frame(
                source, timestamp, frame_path, cancel, webcam_roi(project.settings, source.id)
            )
            frame = cv2.imdecode(np.frombuffer(frame_path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                raise MediaError("Cannot decode Photoshop canvas background")
            frame = without_webcam(frame.astype(np.uint8), webcam_roi(project.settings, source_id))
            if canvas is not None and source_id not in project.rois:
                canvas = inset_canvas(canvas, frame.shape[1], frame.shape[0])
            backdrops[source_id, timestamp] = (
                canvas, canvas_background(frame.astype(np.uint8), canvas),
            )
            samples.setdefault(source_id, []).append((timestamp, box))
            if canvas is not None:
                canvases.setdefault(source_id, []).append(canvas)
            chosen.append((source_id, timestamp, boundaries[slot + 1] - boundaries[slot]))
            if progress:
                progress(0.35 + 0.65 * (slot + 1) / slots)

    decisions: list[RenderDecision] = []
    for source in project.sources:
        position = 0
        for source_id, timestamp, count in chosen:
            if source_id != source.id:
                continue
            if timestamp > position:
                decisions.append(_omission(source.id, position, timestamp))
            decisions.append(RenderDecision(
                source_id=source.id, source_start_us=timestamp, source_end_us=timestamp + 1,
                action="HOLD", output_frame_count=count, provenance="auto:beat_still",
            ))
            position = timestamp + 1
        if position < source.duration_us:
            decisions.append(_omission(source.id, position, source.duration_us))
    result = base.model_copy(update={
        "decisions": decisions, "actual_frame_count": base.target_frame_count,
        "settings": base.settings | {
            "width": 1080, "height": 1920, "story": "photoshop_beat_stills",
            "camera": "drawing_centered", "bookends": False, "quiet_hold_zoom_max": 1.0,
        },
        "warnings": ["Photoshop still selection and musical accents are heuristic; review output"],
    })
    result = apply_square_layout(project, result)
    cameras: dict[tuple[str, int], SubjectCamera] = {}
    for source_id, measured in samples.items():
        source = sources[source_id]
        viewport = drawing_viewport(canvases.get(source_id, []), project.rois.get(source_id))
        width, height = source.width, source.height
        if source.rotation % 180 == 90:
            width, height = height, width
        if uses_square(project.settings):
            crop = next(d.crop for d in result.decisions if d.source_id == source_id)
            if not isinstance(crop, Roi):
                raise ValueError("Photoshop square frame missing")
            square_camera = SubjectCamera(
                viewport=safe_viewport([viewport], crop), fit_subject=True,
                width=crop.width, height=crop.height, background=32,
                points=[CameraPoint(time_us=0, x=crop.x + crop.width / 2,
                                    y=crop.y + crop.height / 2)],
            )
            cameras.update({(source_id, t): square_camera for t, _ in measured})
        else:
            for t, camera in centered_still_cameras(
                measured, width, height, project.settings.shorts_tracking_margin, viewport,
            ).items():
                canvas, colour = backdrops[source_id, t]
                cameras[source_id, t] = camera.model_copy(update={
                    "viewport": project.rois.get(source_id) or canvas or viewport,
                    "background_rgb": colour,
                })
    result = result.model_copy(update={"decisions": [
        d.model_copy(update={"subject_camera": cameras[(d.source_id, d.source_start_us)]})
        if d.action != "DROP" else d for d in result.decisions
    ]})
    validate_plan(project, result)
    return result


def _omission(source_id: str, start: int, end: int) -> RenderDecision:
    return RenderDecision(source_id=source_id, source_start_us=start, source_end_us=end,
                          action="DROP", output_frame_count=0, provenance="auto:beat_omission")


def canvas_background(frame: NDArray[np.uint8], canvas: Roi | None) -> tuple[int, int, int]:
    """Extend the dominant canvas colour, without introducing another artwork copy."""
    if canvas is None:
        return (32, 32, 32)
    h, w = frame.shape[:2]
    x, y, right, bottom = pixel_bounds(canvas, w, h)
    pixels = frame[y:bottom, x:right].reshape(-1, 3)
    quantized = pixels // 16
    codes = quantized[:, 0].astype(int) * 256 + quantized[:, 1].astype(int) * 16 + quantized[:, 2]
    dominant = int(np.bincount(codes).argmax())
    bgr = np.median(pixels[codes == dominant], axis=0)
    return int(bgr[2]), int(bgr[1]), int(bgr[0])


def inset_canvas(canvas: Roi, width: int, height: int) -> Roi:
    """Exclude the same resampled edge from rendered pixels as from detection."""
    edge = max(1, round(min(canvas.width*width, canvas.height*height)
                        * DRAWING_SETTINGS.canvas_border_fraction))
    dx = min(edge / width, canvas.width / 4)
    dy = min(edge / height, canvas.height / 4)
    return Roi(x=canvas.x+dx, y=canvas.y+dy,
               width=canvas.width-2*dx, height=canvas.height-2*dy)


def _measure_frame(
    source: SourceMedia, timestamp: int, destination: Path, cancel: Event | None,
    excluded: Roi | None = None,
) -> tuple[Roi | None, Roi | None]:
    still_frame(source, timestamp, destination, cancel, SETTINGS.sample_width)
    if not destination.is_file():
        raise MediaError("Cannot decode Photoshop music candidate")
    frame = cv2.imdecode(np.frombuffer(destination.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise MediaError("Cannot decode Photoshop music candidate")
    return drawing_and_canvas_box(without_webcam(frame.astype(np.uint8), excluded))
