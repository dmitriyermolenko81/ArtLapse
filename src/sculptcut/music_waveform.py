"""Bounded soundtrack envelopes on the final output clock, independent of Qt."""

import math
import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import numpy as np
from numpy.typing import NDArray

from sculptcut.audio import music_filter
from sculptcut.domain import ProjectSettings
from sculptcut.intro import body_start_frame, output_frame_count
from sculptcut.media import Cancelled, discover, run_process
from sculptcut.music_library import soundtrack
from sculptcut.planning import RenderPlan
from sculptcut.project import cache_key


@dataclass(frozen=True)
class WaveSettings:
    sample_rate: int = 4000
    minimum_step_seconds: float = 0.01
    maximum_bins: int = 120_000
    peak_minimum: float = 0.2
    peak_prominence: float = 0.1
    peak_baseline_seconds: float = 0.3
    peak_spacing_seconds: float = 0.18


SETTINGS = WaveSettings()


@dataclass(frozen=True)
class MusicWave:
    duration: float
    step: float
    levels: NDArray[np.float32]
    accents: tuple[float, ...]


@dataclass(frozen=True)
class OutputEpisode:
    row: int
    start: float
    end: float


def output_episodes(settings: ProjectSettings, plan: RenderPlan) -> list[OutputEpisode]:
    position = body_start_frame(settings, plan.output_fps, plan.actual_frame_count)
    result = []
    for row, d in enumerate(plan.decisions):
        if d.output_frame_count:
            pieces = len(d.sample_times_us) if d.action == "MONTAGE" else 1
            for index in range(pieces):
                result.append(OutputEpisode(
                    row, (position + d.output_frame_count * index // pieces) / plan.output_fps,
                    (position + d.output_frame_count * (index + 1) // pieces) / plan.output_fps,
                ))
            position += d.output_frame_count
    return result


def amplitude_accents(levels: NDArray[np.float32], step: float) -> tuple[float, ...]:
    if len(levels) < 3 or float(levels.max()) <= 0:
        return ()
    width = min(len(levels), max(3, round(SETTINGS.peak_baseline_seconds / step)))
    baseline = np.convolve(levels, np.ones(width) / width, mode="same")
    candidates = [i for i in range(1, len(levels) - 1)
                  if levels[i] >= SETTINGS.peak_minimum
                  and levels[i] - baseline[i] >= SETTINGS.peak_prominence
                  and levels[i] > levels[i - 1] and levels[i] >= levels[i + 1]]
    selected: list[int] = []
    for index in candidates:
        if selected and (index - selected[-1]) * step < SETTINGS.peak_spacing_seconds:
            if levels[index] > levels[selected[-1]]:
                selected[-1] = index
        else:
            selected.append(index)
    return tuple((index + 0.5) * step for index in selected)


def build_music_wave(root: Path, settings: ProjectSettings, plan: RenderPlan,
                     cancel: Event) -> MusicWave | None:
    duration = (output_frame_count(settings, plan.output_fps, plan.actual_frame_count)
                / plan.output_fps)
    music = soundtrack(root, settings, duration, cancel)
    if music is None:
        return None
    stat = music.stat()
    key = cache_key("music-wave-1", str(music.resolve()), stat.st_size, stat.st_mtime_ns,
                    duration, settings.music_loop, settings.music_fade_seconds, SETTINGS.__dict__)
    directory = root / "cache" / "music-wave"
    directory.mkdir(parents=True, exist_ok=True)
    cached = directory / f"{key}.npz"
    if cancel.is_set():
        raise Cancelled("Waveform cancelled")
    if cached.is_file():
        try:
            with np.load(cached, allow_pickle=False) as data:
                levels = np.asarray(data["levels"], dtype=np.float32)
                step = float(data["step"])
                if (levels.ndim == 1 and 0 < len(levels) <= SETTINGS.maximum_bins
                        and np.all(np.isfinite(levels)) and step > 0):
                    return MusicWave(duration, step, levels, amplitude_accents(levels, step))
        except (OSError, ValueError, KeyError):
            pass
    block = max(round(SETTINGS.sample_rate * SETTINGS.minimum_step_seconds),
                math.ceil(duration * SETTINGS.sample_rate / SETTINGS.maximum_bins))
    step = block / SETTINGS.sample_rate
    parts = []
    with tempfile.TemporaryDirectory(prefix="wave-", dir=directory) as folder:
        pcm = Path(folder) / "audio.f32"
        run_process([
            discover("ffmpeg"), "-v", "error", "-nostdin",
            *(["-stream_loop", "-1"] if settings.music_loop else []), "-i", str(music),
            "-map", "0:a:0", "-vn", "-af", music_filter(duration, settings.music_fade_seconds),
            "-ac", "1", "-ar", str(SETTINGS.sample_rate), "-t", f"{duration:.9f}",
            "-f", "f32le", str(pcm),
        ], cancel)
        with pcm.open("rb") as stream:
            while True:
                if cancel.is_set():
                    raise Cancelled("Waveform cancelled")
                samples = np.fromfile(stream, dtype="<f4", count=block * 256)
                if not len(samples):
                    break
                samples = np.pad(samples, (0, (-len(samples)) % block))
                parts.append(np.max(np.abs(samples.reshape(-1, block)), axis=1))
        levels = (np.concatenate(parts).astype(np.float32)
                  if parts else np.zeros(1, dtype=np.float32))
        peak = float(levels.max())
        if peak > 0:
            levels /= peak
        temporary = Path(folder) / "wave.npz"
        np.savez_compressed(temporary, levels=levels, step=step)
        if cancel.is_set():
            raise Cancelled("Waveform cancelled")
        temporary.replace(cached)
    return MusicWave(duration, step, levels, amplitude_accents(levels, step))
