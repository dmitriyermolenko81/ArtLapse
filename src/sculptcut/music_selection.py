"""Local soundtrack history and bounded audio-energy selection."""

import json
from pathlib import Path
from threading import Event

import numpy as np

from sculptcut.media import discover, run_process

SAMPLE_RATE = 2000
SCAN_SECONDS = 600
WINDOW_SECONDS = 12


def recent_tracks(directory: Path) -> list[str]:
    history: list[str] = []
    for manifest in sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime_ns):
        try:
            state = json.loads(manifest.read_text(encoding="utf-8"))
            for track in state["tracks"]:
                path = str(track["path"])
                if path in history:
                    history.remove(path)
                history.append(path)
        except (ValueError, KeyError, TypeError):
            continue
    return history


def energetic_start(
    path: Path, duration: float, needed: float, scratch: Path, cancel: Event | None
) -> float:
    # Keep enough music after the chosen point, including for long timelapses.
    available = min(duration, SCAN_SECONDS)
    latest = min(duration - min(needed, max(20.0, duration / 2)), available - WINDOW_SECONDS)
    if latest < 1:
        return 0.0
    pcm = scratch / "energy.f32"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(path),
            "-t",
            str(available),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "f32le",
            str(pcm),
        ],
        cancel,
    )
    samples = np.fromfile(pcm, dtype="<f4")  # At most 600 * 2000 floats (4.8 MB).
    pcm.unlink()
    seconds = len(samples) // SAMPLE_RATE
    if seconds < WINDOW_SECONDS:
        return 0.0
    energy = np.mean(samples[: seconds * SAMPLE_RATE].reshape(seconds, SAMPLE_RATE) ** 2, axis=1)
    scores = np.convolve(energy, np.ones(WINDOW_SECONDS) / WINDOW_SECONDS, mode="valid")
    scores = scores[: int(latest) + 1]
    if not len(scores) or not np.all(np.isfinite(scores)):
        return 0.0
    return float(np.argmax(scores))
