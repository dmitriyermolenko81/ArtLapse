"""Score candidate Photoshop episodes for a nearly stationary canvas."""

import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from types import TracebackType

import cv2
import numpy as np
from numpy.typing import NDArray

from sculptcut.domain import Project, Roi, SourceMedia
from sculptcut.media import Cancelled, MediaError, discover, run_process
from sculptcut.photoshop import drawing_and_canvas_box, drawing_box
from sculptcut.project import cache_get, cache_key, cache_put
from sculptcut.roi import pixel_bounds
from sculptcut.visual_transitions import SETTINGS as VIEW_SETTINGS
from sculptcut.webcam import AnalysisCapture, webcam_roi


@dataclass(frozen=True)
class StabilitySettings:
    fps: int = 1
    width: int = 480
    candidates: int = 5
    activity_delta: int = 12
    minimum_activity: float = 0.001
    missing_penalty: float = 0.4
    quiet_penalty: float = 0.1


SETTINGS = StabilitySettings()


class DrawingViews:
    """Bounded canvas-only signatures, with one reusable reader per source."""

    def __init__(self, project: Project, cancel: Event | None = None) -> None:
        self.project, self.cancel = project, cancel
        self.readers: dict[str, AnalysisCapture] = {}
        self.cache: dict[tuple[str, int], NDArray[np.uint8] | None] = {}

    def __enter__(self) -> "DrawingViews":
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        for reader in self.readers.values():
            reader.release()

    def signature(self, source: SourceMedia, time_us: int) -> NDArray[np.uint8] | None:
        if self.cancel and self.cancel.is_set():
            raise Cancelled("Drawing transition inspection cancelled")
        key = (source.id, time_us)
        if key in self.cache:
            return self.cache[key]
        reader = self.readers.get(source.id)
        if reader is None:
            reader = AnalysisCapture(source.path, webcam_roi(self.project.settings, source.id))
            self.readers[source.id] = reader
        reader.set(cv2.CAP_PROP_POS_MSEC, time_us / 1000)
        ok, frame = reader.read()
        view = None
        if ok:
            h, w = frame.shape[:2]
            width = min(w, VIEW_SETTINGS.read_width)
            image = cv2.resize(frame, (width, max(1, round(h * width / w)))).astype(np.uint8)
            canvas = self.project.rois.get(source.id)
            if canvas is None:
                _, canvas = drawing_and_canvas_box(image)
            if canvas is not None:
                x, y, right, bottom = pixel_bounds(canvas, image.shape[1], image.shape[0])
                region = cv2.cvtColor(image[y:bottom, x:right], cv2.COLOR_BGR2GRAY)
                view = cv2.resize(region, (VIEW_SETTINGS.signature_size,
                                          VIEW_SETTINGS.signature_size),
                                  interpolation=cv2.INTER_AREA).astype(np.uint8)
        self.cache[key] = view
        if len(self.cache) > VIEW_SETTINGS.cache_entries:
            self.cache.pop(next(iter(self.cache)))
        return view


def stability_score(frames: Iterable[NDArray[np.uint8]]) -> float:
    """Lower means less pan/zoom; avoid mistaking a completely still frame for work."""
    centers: list[tuple[float, float]] = []
    widths: list[float] = []
    heights: list[float] = []
    activity: list[float] = []
    previous_gray: NDArray[np.uint8] | None = None
    previous_box: Roi | None = None
    total = 0
    for frame in frames:
        total += 1
        box = drawing_box(frame, previous_box)
        if box is not None:
            previous_box = box
            centers.append((box.x + box.width / 2, box.y + box.height / 2))
            widths.append(box.width)
            heights.append(box.height)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.uint8)
        if previous_gray is not None:
            h, w = gray.shape
            core = np.s_[h // 8 : 7 * h // 8, w // 8 : 7 * w // 8]
            activity.append(
                float(
                    np.mean(cv2.absdiff(gray[core], previous_gray[core]) > SETTINGS.activity_delta)
                )
            )
        previous_gray = gray
    if total < 2:
        return 10.0
    if len(centers) < max(2, total // 2):
        return 5.0 + SETTINGS.missing_penalty * (total - len(centers)) / total
    position = np.asarray(centers)
    pan = float(np.ptp(position[:, 0]) + np.ptp(position[:, 1]))
    zoom = float(np.log(max(widths) / min(widths)) + np.log(max(heights) / min(heights)))
    missing = SETTINGS.missing_penalty * (total - len(centers)) / total
    motion = 2 * pan + zoom + missing
    if np.mean(activity) < SETTINGS.minimum_activity:
        motion += SETTINGS.quiet_penalty
    return motion


def score_source_window(
    root: Path,
    source: SourceMedia,
    start_us: int,
    end_us: int,
    cancel: Event | None,
    excluded: Roi | None = None,
) -> float:
    key = cache_key(
        "photoshop-stability-1", source.fingerprint, start_us, end_us, SETTINGS, excluded
    )
    cached = cache_get(root, "photoshop_stability", source.id, key)
    if cached is not None:
        return float(cached)
    with tempfile.TemporaryDirectory(prefix="sculptcut-photoshop-stability-") as directory:
        clip = Path(directory) / "candidate.mp4"
        run_process(
            [
                discover("ffmpeg"),
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-ss",
                f"{start_us / 1e6:.6f}",
                "-i",
                str(source.path),
                "-map",
                f"0:{source.stream_index}",
                "-an",
                "-t",
                f"{(end_us - start_us) / 1e6:.6f}",
                "-vf",
                f"fps={SETTINGS.fps},scale='min({SETTINGS.width},iw)':-2",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "22",
                str(clip),
            ],
            cancel,
        )
        reader = AnalysisCapture(clip, excluded)
        try:

            def frames() -> Iterable[NDArray[np.uint8]]:
                while True:
                    if cancel and cancel.is_set():
                        raise Cancelled("Photoshop stability analysis cancelled")
                    ok, frame = reader.read()
                    if not ok:
                        break
                    yield frame.astype(np.uint8, copy=False)

            if not reader.isOpened():
                raise MediaError("Cannot decode Photoshop stability candidate")
            score = stability_score(frames())
        finally:
            reader.release()
    cache_put(root, "photoshop_stability", source.id, key, score)
    return score
