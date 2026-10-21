"""Conservative removal of visually unchanged Photoshop source intervals."""

import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import cv2
import numpy as np
from numpy.typing import NDArray

from sculptcut.domain import Project, Roi, SourceMedia
from sculptcut.idle import IdleRange
from sculptcut.media import Cancelled, MediaError, discover, run_process
from sculptcut.project import cache_get, cache_key, cache_put
from sculptcut.webcam import AnalysisCapture, webcam_roi


@dataclass(frozen=True)
class PhotoshopIdleSettings:
    fps: int = 2
    width: int = 480
    minimum_us: int = 15_000_000
    padding_us: int = 1_500_000
    pixel_delta: int = 12
    maximum_changed_ratio: float = 0.003


SETTINGS = PhotoshopIdleSettings()


def detect_photoshop_idle(
    frames: Iterable[NDArray[np.uint8]],
    source_id: str,
    duration_us: int,
    settings: PhotoshopIdleSettings = SETTINGS,
) -> list[IdleRange]:
    """Require both adjacent and cumulative visual stability before dropping time."""
    result: list[IdleRange] = []
    previous: NDArray[np.uint8] | None = None
    anchor: NDArray[np.uint8] | None = None
    quiet_start: int | None = None
    last_time = -1

    def finish(end_us: int) -> None:
        if quiet_start is not None and end_us - quiet_start >= settings.minimum_us:
            start = quiet_start + settings.padding_us
            end = min(duration_us, end_us - settings.padding_us)
            if start < end:
                result.append(
                    IdleRange(
                        source_id=source_id,
                        start_us=start,
                        end_us=end,
                        reason="photoshop:visually_static",
                    )
                )

    for index, frame in enumerate(frames):
        time_us = round(index * 1_000_000 / settings.fps)
        if time_us >= duration_us:
            break
        last_time = time_us
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.uint8)
        if previous is not None:
            reference = anchor if anchor is not None else previous
            adjacent = float(np.mean(cv2.absdiff(gray, previous) > settings.pixel_delta))
            cumulative = float(np.mean(cv2.absdiff(gray, reference) > settings.pixel_delta))
            quiet = max(adjacent, cumulative) <= settings.maximum_changed_ratio
            if quiet:
                if quiet_start is None:
                    quiet_start = time_us
                    anchor = previous
            else:
                finish(time_us)
                quiet_start = None
                anchor = None
        previous = gray
    if last_time < 0:
        raise MediaError("Cannot decode Photoshop idle analysis clip")
    finish(min(duration_us, last_time + round(1_000_000 / settings.fps)))
    return result


def source_idle(
    source: SourceMedia,
    cancel: Event | None,
    settings: PhotoshopIdleSettings = SETTINGS,
    excluded: Roi | None = None,
) -> list[IdleRange]:
    with tempfile.TemporaryDirectory(prefix="sculptcut-photoshop-idle-") as directory:
        clip = Path(directory) / "analysis.mp4"
        run_process(
            [
                discover("ffmpeg"),
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(source.path),
                "-map",
                f"0:{source.stream_index}",
                "-an",
                "-vf",
                f"setpts=PTS-STARTPTS,fps={settings.fps},scale='min({settings.width},iw)':-2",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                str(clip),
            ],
            cancel,
        )
        reader = AnalysisCapture(clip, excluded)
        try:

            def frames() -> Iterable[NDArray[np.uint8]]:
                while True:
                    if cancel and cancel.is_set():
                        raise Cancelled("Photoshop idle analysis cancelled")
                    ok, frame = reader.read()
                    if not ok:
                        break
                    yield frame.astype(np.uint8, copy=False)

            return detect_photoshop_idle(frames(), source.id, source.duration_us, settings)
        finally:
            reader.release()


def project_photoshop_idle(
    root: Path,
    project: Project,
    cancel: Event | None,
    progress: Callable[[float], None] | None = None,
) -> list[IdleRange]:
    ranges: list[IdleRange] = []
    for index, source in enumerate(project.sources):
        if cancel and cancel.is_set():
            raise Cancelled("Photoshop idle analysis cancelled")
        key = cache_key("photoshop-idle-1", source.fingerprint, SETTINGS,
                        webcam_roi(project.settings, source.id))
        cached = cache_get(root, "photoshop_idle", source.id, key)
        if cached is None:
            found = source_idle(source, cancel, excluded=webcam_roi(project.settings, source.id))
            cache_put(root, "photoshop_idle", source.id, key, [r.model_dump() for r in found])
        else:
            found = [IdleRange.model_validate(r) for r in cached]
        ranges.extend(found)
        if progress:
            progress((index + 1) / len(project.sources))
    return ranges
