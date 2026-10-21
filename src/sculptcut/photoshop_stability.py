"""Score candidate Photoshop episodes for a nearly stationary canvas."""

import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import cv2
import numpy as np
from numpy.typing import NDArray

from sculptcut.domain import Roi, SourceMedia
from sculptcut.media import Cancelled, MediaError, discover, run_process
from sculptcut.photoshop import drawing_box
from sculptcut.project import cache_get, cache_key, cache_put
from sculptcut.webcam import AnalysisCapture


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
