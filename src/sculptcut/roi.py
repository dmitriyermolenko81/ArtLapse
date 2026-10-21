"""Normalized analysis regions and human-verifiable overlays."""

from pathlib import Path

import cv2
import numpy as np
from numpy.typing import NDArray

from sculptcut.domain import Project, Roi
from sculptcut.media import MediaError
from sculptcut.project import save_project
from sculptcut.proxy import make_proxies, proxy_path


def pixel_bounds(roi: Roi, width: int, height: int) -> tuple[int, int, int, int]:
    if width <= 0 or height <= 0:
        raise ValueError("Image dimensions must be positive")
    x = min(width - 1, int(roi.x * width))
    y = min(height - 1, int(roi.y * height))
    right = min(width, max(x + 1, round((roi.x + roi.width) * width)))
    bottom = min(height, max(y + 1, round((roi.y + roi.height) * height)))
    return x, y, right, bottom


def masks(roi: Roi, width: int, height: int) -> tuple[NDArray[np.bool_], NDArray[np.bool_]]:
    canvas = np.zeros((height, width), dtype=np.bool_)
    x, y, right, bottom = pixel_bounds(roi, width, height)
    canvas[y:bottom, x:right] = True
    return canvas, ~canvas


def set_roi(root: Path, project: Project, source_id: str, roi: Roi) -> Path:
    source = next((s for s in project.sources if s.id == source_id), None)
    if source is None:
        raise ValueError("Unknown source ID")
    make_proxies(root, project)
    capture = cv2.VideoCapture(str(proxy_path(root, project, source)))
    try:
        ok, frame = capture.read()
        if not ok:
            raise MediaError("Cannot read proxy for ROI overlay")
        x, y, right, bottom = pixel_bounds(roi, frame.shape[1], frame.shape[0])
        cv2.rectangle(frame, (x, y), (right - 1, bottom - 1), (0, 255, 0), 2)
        ok, png = cv2.imencode(".png", frame)
        if not ok:
            raise MediaError("Cannot encode ROI overlay")
        destination = root / "thumbnails" / f"{source_id}-roi.png"
        png.tofile(str(destination))
    finally:
        capture.release()
    updated = Project.model_validate(
        project.model_dump() | {"rois": project.rois | {source_id: roi}}
    )
    save_project(root, updated)
    return destination
