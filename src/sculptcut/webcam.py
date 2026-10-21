"""Remove a user-marked overlay from analysis copies, never from rendered media."""

from pathlib import Path
from typing import Any

import cv2
import numpy as np
from numpy.typing import NDArray

from sculptcut.domain import Project, ProjectSettings, Roi, SourceMedia
from sculptcut.project import cache_key
from sculptcut.proxy import proxy_key
from sculptcut.roi import pixel_bounds


def webcam_roi(settings: ProjectSettings, source_id: str) -> Roi | None:
    return settings.webcam_rois.get(source_id) if settings.webcam_enabled else None


def analysis_media_key(project: Project, source: SourceMedia) -> str:
    roi = webcam_roi(project.settings, source.id)
    original = proxy_key(project, source)
    return cache_key(original, "webcam-1", roi.model_dump()) if roi else original


def without_webcam(frame: NDArray[np.uint8], roi: Roi | None) -> NDArray[np.uint8]:
    if roi is None:
        return frame
    h, w = frame.shape[:2]
    x, y, right, bottom = pixel_bounds(roi, w, h)
    valid = np.ones((h, w), dtype=bool)
    valid[y:bottom, x:right] = False
    if not valid.any():
        raise ValueError("Webcam exclusion must leave part of the image visible")
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    # Fill from the remaining image's dominant background; excluded pixels never
    # contribute, even to the fill colour. No temporal state or source mutation.
    mode = int(np.bincount((gray[valid] // 4).ravel(), minlength=64).argmax())
    background = valid & (gray // 4 == mode)
    colour = np.median(frame[background], axis=0).astype(np.uint8)
    result = frame.copy()
    result[y:bottom, x:right] = colour
    return result


class AnalysisCapture:
    def __init__(self, path: str | Path, roi: Roi | None = None) -> None:
        self.capture = cv2.VideoCapture(str(path))
        self.excluded = roi

    def read(self, image: Any = None) -> tuple[bool, Any]:
        ok, frame = self.capture.read(image)
        return ok, without_webcam(frame, self.excluded) if ok else frame


    def get(self, prop: int) -> float:
        return self.capture.get(prop)

    def set(self, prop: int, value: float) -> bool:
        return self.capture.set(prop, value)

    def isOpened(self) -> bool:
        return self.capture.isOpened()

    def grab(self) -> bool:
        return self.capture.grab()

    def release(self) -> None:
        self.capture.release()
