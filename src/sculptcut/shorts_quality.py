"""Bounded visual checks for ordinary Shorts work episodes, independent of beat stills."""

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from types import TracebackType

import cv2
import numpy as np
from numpy.typing import NDArray

from sculptcut.development import SETTINGS as DEVELOPMENT
from sculptcut.development import DevelopmentEvidence, Window, measure_development
from sculptcut.domain import Project, Roi
from sculptcut.media import Cancelled, MediaError
from sculptcut.proxy import proxy_path
from sculptcut.roi import pixel_bounds
from sculptcut.screen_cleanup import screen_kind
from sculptcut.shorts import sculpt_footprint, viewport_box
from sculptcut.webcam import AnalysisCapture, webcam_roi


@dataclass(frozen=True)
class EpisodeQuality:
    samples: int = 9
    edge_margin: float = 0.025
    maximum_center_travel: float = 0.10
    maximum_scale_ratio: float = 1.35
    maximum_view_change: float = 0.12
    minimum_observed_fraction: float = 0.7
    uncertain_quality: float = 0.25
    maximum_forward_decode_seconds: float = 5


QUALITY = EpisodeQuality()


def proxy_sample(
    reader: cv2.VideoCapture | AnalysisCapture, time_us: float, fps: float,
    cancel: Event | None = None,
) -> NDArray[np.uint8] | None:
    """Read the same CFR sample as an OpenCV seek, avoiding nearby forward seeks."""
    target = int(time_us * fps / 1_000_000 + 0.5)
    current = round(reader.get(cv2.CAP_PROP_POS_FRAMES))
    if 0 <= target - current <= fps * QUALITY.maximum_forward_decode_seconds:
        for _ in range(target - current):
            if cancel and cancel.is_set():
                raise Cancelled("Shorts episode selection cancelled")
            if not reader.grab():
                return None
    else:
        reader.set(cv2.CAP_PROP_POS_MSEC, time_us / 1000)
    if cancel and cancel.is_set():
        raise Cancelled("Shorts episode selection cancelled")
    ok, frame = reader.read()
    return np.asarray(frame, dtype=np.uint8) if ok else None


def stable_episode_score(frames: list[NDArray[np.uint8]]) -> float:
    """Prefer a readable run on one detail; reject pans, zooms, menus and cutoffs."""
    if not frames:
        return 0.0
    boxes: list[Roi] = []
    images: list[NDArray[np.uint8]] = []
    for frame in frames:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.uint8)
        if screen_kind(gray):
            return 0.0
        viewport = viewport_box(gray)
        if viewport is None:
            continue
        box = sculpt_footprint(gray, viewport)
        if box is None:
            continue
        x = (box.x - viewport.x) / viewport.width
        y = (box.y - viewport.y) / viewport.height
        width = box.width / viewport.width
        height = box.height / viewport.height
        margin = QUALITY.edge_margin
        # A bust often continues below the ZBrush canvas. Its lower edge may
        # touch the canvas while the face remains fully visible and readable.
        if x < margin or y < margin or x + width > 1 - margin:
            return 0.0
        if y + height > 1 + 1e-6:
            return 0.0
        boxes.append(Roi(x=x, y=y, width=width, height=min(height, 1 - y)))
        h, w = gray.shape
        region = gray[
            round(viewport.y * h) : round((viewport.y + viewport.height) * h),
            round(viewport.x * w) : round((viewport.x + viewport.width) * w),
        ]
        images.append(cv2.resize(region, (96, 96)).astype(np.uint8))
    if len(boxes) < len(frames) * QUALITY.minimum_observed_fraction:
        return QUALITY.uncertain_quality
    travel = max(
        float(np.ptp([b.x + b.width / 2 for b in boxes])),
        float(np.ptp([b.y + b.height / 2 for b in boxes])),
    )
    scale = max(
        max(b.width for b in boxes) / min(b.width for b in boxes),
        max(b.height for b in boxes) / min(b.height for b in boxes),
    )
    view_change = max(
        float(np.mean(cv2.absdiff(images[0], image).astype(np.float32))) / 255 for image in images
    )
    if (
        travel > QUALITY.maximum_center_travel
        or scale > QUALITY.maximum_scale_ratio
        or view_change > QUALITY.maximum_view_change
    ):
        return 0.0
    return 1 / (1 + travel * 10 + (scale - 1) * 3 + view_change * 5)


class EpisodeInspector:
    def __init__(self, root: Path, project: Project, cancel: Event | None = None) -> None:
        self.root, self.project, self.cancel = root, project, cancel
        self.readers: dict[str, cv2.VideoCapture | AnalysisCapture] = {}
        self.inspections: OrderedDict[Window, tuple[float, DevelopmentEvidence]] = OrderedDict()

    def __enter__(self) -> "EpisodeInspector":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        for reader in self.readers.values():
            reader.release()

    def score(self, source_id: str, start: int, end: int) -> float:
        if self.cancel and self.cancel.is_set():
            raise Cancelled("Episode inspection cancelled")
        window = (source_id, start, end)
        cached = self.inspections.get(window)
        if cached is not None:
            self.inspections.move_to_end(window)
            return cached[0]
        reader = self.readers.get(source_id)
        if reader is None:
            source = next(s for s in self.project.sources if s.id == source_id)
            path = proxy_path(self.root, self.project, source)
            reader = AnalysisCapture(path, webcam_roi(self.project.settings, source.id))
            self.readers[source_id] = reader
            if not reader.isOpened():
                raise MediaError(f"Cannot inspect Shorts episodes: {path}")
        frames = []
        for timestamp in np.linspace(start, max(start, end - 200_000), QUALITY.samples):
            if self.cancel and self.cancel.is_set():
                raise Cancelled("Shorts episode selection cancelled")
            frame = proxy_sample(reader, float(timestamp), reader.get(cv2.CAP_PROP_FPS),
                                 self.cancel)
            if frame is None:
                return 0.0
            h, w = frame.shape[:2]
            frames.append(cv2.resize(frame, (480, max(1, round(h * 480 / w)))).astype(np.uint8))
        quality = stable_episode_score(frames)
        # In sculpt-only mode an unrecognized canvas may be an extreme close-up.
        # Do not re-admit that shot through the generic application fallback.
        if self.project.settings.sculpt_only and quality == QUALITY.uncertain_quality:
            quality = 0.0
        evidence = DevelopmentEvidence()
        roi = self.project.rois.get(source_id)
        if roi is None:
            roi = viewport_box(cv2.cvtColor(frames[0], cv2.COLOR_BGR2GRAY).astype(np.uint8))
        # Timelapses may intentionally show a close-up that Shorts framing
        # rejects. Appearance evidence has its own motion/comparability gates.
        distinct_frames = (end - start) * reader.get(cv2.CAP_PROP_FPS) / 1_000_000
        if roi is not None and distinct_frames >= DEVELOPMENT.minimum_frames and not any(
            screen_kind(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.uint8))
            for frame in frames
        ):
            samples = []
            for frame in frames:
                x, y, right, bottom = pixel_bounds(roi, frame.shape[1], frame.shape[0])
                gray = cv2.cvtColor(frame[y:bottom, x:right], cv2.COLOR_BGR2GRAY)
                scale = DEVELOPMENT.width / max(gray.shape)
                resized = cv2.resize(gray, (
                    max(1, round(gray.shape[1] * scale)), max(1, round(gray.shape[0] * scale)),
                ))
                samples.append(cv2.GaussianBlur(resized, (3, 3), 0).astype(np.uint8))
            evidence = measure_development(samples)
        self.inspections[window] = (quality, evidence)
        if len(self.inspections) > DEVELOPMENT.cache_entries:
            self.inspections.popitem(last=False)
        return quality

    def development(self, source_id: str, start: int, end: int) -> DevelopmentEvidence:
        window = (source_id, start, end)
        self.score(*window)
        cached = self.inspections.get(window)
        return cached[1] if cached is not None else DevelopmentEvidence()

    def signature(self, source_id: str, time_us: int) -> NDArray[np.uint8] | None:
        reader = self.readers[source_id]
        frame = proxy_sample(reader, time_us, reader.get(cv2.CAP_PROP_FPS), self.cancel)
        if frame is None:
            return None
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.uint8)
        viewport = viewport_box(gray)
        if viewport is None:
            return None
        h, w = gray.shape
        region = gray[
            round(viewport.y * h) : round((viewport.y + viewport.height) * h),
            round(viewport.x * w) : round((viewport.x + viewport.width) * w),
        ]
        return cv2.resize(region, (64, 64), interpolation=cv2.INTER_AREA).astype(np.uint8)
