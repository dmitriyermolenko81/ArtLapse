"""Conservative fixed crops; hybrid uses the union of observed object bounds."""

from pathlib import Path
from threading import Event

import cv2
import numpy as np

from sculptcut.analysis import Gray
from sculptcut.domain import Project, Roi, SourceMedia
from sculptcut.media import Cancelled
from sculptcut.planning import RenderPlan
from sculptcut.project import cache_get, cache_key, cache_put
from sculptcut.proxy import proxy_path
from sculptcut.roi import pixel_bounds
from sculptcut.webcam import AnalysisCapture, analysis_media_key, webcam_roi


def object_bounds(gray: Gray) -> Roi | None:
    """Background-contrast hypothesis, rejected near borders or with competing objects."""
    height, width = gray.shape
    background = float(
        np.median(np.concatenate((gray[0, :], gray[-1, :], gray[:, 0], gray[:, -1])))
    )
    mask = (np.abs(gray.astype(float) - background) > 20).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), dtype=np.uint8)).astype(np.uint8)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    ordered = sorted(contours, key=cv2.contourArea, reverse=True)
    if not ordered:
        return None
    area = cv2.contourArea(ordered[0]) / (width * height)
    if not 0.01 <= area <= 0.65:
        return None
    if len(ordered) > 1 and cv2.contourArea(ordered[1]) > cv2.contourArea(ordered[0]) * 0.25:
        return None
    x, y, w, h = cv2.boundingRect(ordered[0])
    margin_x, margin_y = max(8, round(w * 0.2)), max(8, round(h * 0.2))
    left, top, right, bottom = x - margin_x, y - margin_y, x + w + margin_x, y + h + margin_y
    if left <= 0 or top <= 0 or right >= width or bottom >= height:
        return None
    return Roi(
        x=left / width, y=top / height, width=(right - left) / width, height=(bottom - top) / height
    )


def union_bounds(bounds: list[Roi]) -> Roi | None:
    if not bounds:
        return None
    x, y = min(r.x for r in bounds), min(r.y for r in bounds)
    right, bottom = max(r.x + r.width for r in bounds), max(r.y + r.height for r in bounds)
    return Roi(x=x, y=y, width=right - x, height=bottom - y)


def fit_video_aspect(roi: Roi, width: int, height: int, aspect: float = 16 / 9) -> Roi:
    """Keep the chosen center and fit the requested aspect inside the rectangle."""
    if aspect <= 0:
        raise ValueError("Crop aspect must be positive")
    crop_width, crop_height = roi.width, roi.height
    if crop_width * width > crop_height * height * aspect:
        crop_width = crop_height * height * aspect / width
    else:
        crop_height = crop_width * width / (aspect * height)
    x = min(max(0.0, roi.x + (roi.width - crop_width) / 2), 1 - crop_width)
    y = min(max(0.0, roi.y + (roi.height - crop_height) / 2), 1 - crop_height)
    return Roi(x=x, y=y, width=crop_width, height=crop_height)


def hybrid_crop(
    root: Path, project: Project, source: SourceMedia, cancel: Event | None
) -> Roi | None:
    canvas = project.rois.get(source.id)
    if canvas is None:
        return None
    key = cache_key("crop-1", analysis_media_key(project, source), canvas.model_dump())
    cached = cache_get(root, "crop", source.id, key)
    if cached:
        return Roi.model_validate(cached["roi"]) if cached["roi"] else None
    capture = AnalysisCapture(
        proxy_path(root, project, source), webcam_roi(project.settings, source.id)
    )
    combined: Roi | None = None
    samples = 0
    first_frame = None
    confident = True
    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled("Crop analysis cancelled")
            ok, frame = capture.read()
            if not ok:
                break
            if first_frame is None:
                first_frame = frame.copy()
            x, y, right, bottom = pixel_bounds(canvas, frame.shape[1], frame.shape[0])
            gray = cv2.cvtColor(frame[y:bottom, x:right], cv2.COLOR_BGR2GRAY).astype(np.uint8)
            bounds = object_bounds(gray)
            samples += 1
            if bounds is None:
                confident = False
                break
            global_bounds = Roi(
                x=canvas.x + bounds.x * canvas.width,
                y=canvas.y + bounds.y * canvas.height,
                width=bounds.width * canvas.width,
                height=bounds.height * canvas.height,
            )
            combined = union_bounds([combined, global_bounds] if combined else [global_bounds])
    finally:
        capture.release()
    result = combined if confident and samples >= 3 else None
    if result and first_frame is not None:
        x, y, right, bottom = pixel_bounds(result, first_frame.shape[1], first_frame.shape[0])
        cv2.rectangle(first_frame, (x, y), (right - 1, bottom - 1), (0, 255, 0), 2)
        ok, png = cv2.imencode(".png", first_frame)
        if ok:
            png.tofile(str(root / "thumbnails" / f"{source.id}-crop.png"))
    cache_put(root, "crop", source.id, key, {"roi": result.model_dump() if result else None})
    return result


def apply_crop(root: Path, project: Project, plan: RenderPlan, cancel: Event | None) -> RenderPlan:
    if project.settings.crop_mode == "full":
        return plan
    if project.settings.crop_mode == "viewport" and project.settings.preset != "timelapse":
        raise ValueError("Viewport crop is available only for the timelapse preset")
    crops = {}
    warnings = plan.warnings[:]
    for source in project.sources:
        if project.settings.crop_mode == "canvas":
            if source.id not in project.rois:
                raise ValueError("Canvas mode requires an explicit ROI for each source")
            crops[source.id] = project.rois[source.id]
        elif project.settings.crop_mode == "viewport":
            width, height = source.width, source.height
            if source.rotation % 180 == 90:
                width, height = height, width
            crops[source.id] = fit_video_aspect(
                project.settings.timelapse_viewport_roi, width, height
            )
        else:
            roi = hybrid_crop(root, project, source, cancel)
            if roi is None:
                warnings.append(f"Hybrid fallback to Full UI: {source.id}")
            else:
                crops[source.id] = roi
    decisions = [
        d.model_copy(update={"crop": crops.get(d.source_id, "full")}) for d in plan.decisions
    ]
    return plan.model_copy(
        update={
            "decisions": decisions,
            "warnings": warnings,
            "settings": plan.settings | {"mode": project.settings.crop_mode},
        }
    )
