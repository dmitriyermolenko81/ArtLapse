"""Beat grid and local intensity for music-driven still-frame editing."""

import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import numpy as np
from numpy.typing import NDArray

from sculptcut.beat_tracking import track_beats
from sculptcut.media import Cancelled, discover, run_process


@dataclass(frozen=True)
class BeatAnalysisSettings:
    sample_rate: int = 11_025
    window_size: int = 2_048
    hop_size: int = 256
    onset_baseline_seconds: float = 1.0
    intensity_window_seconds: float = 0.5
    silence_fraction: float = 0.08
    onset_density_seconds: float = 1.5
    density_weight: float = 0.65
    onset_peak_quantile: float = 0.65
    foreground_low_hz: int = 45
    foreground_mid_hz: int = 300
    foreground_high_hz: int = 3_000
    foreground_top_hz: int = 4_800
    foreground_low_penalty: float = 0.25
    foreground_high_penalty: float = 0.15
    foreground_mid_low_ratio: float = 1.3
    foreground_mid_high_ratio: float = 3.0
    foreground_smooth_frames: int = 3
    foreground_baseline_seconds: float = 0.25
    foreground_baseline_fraction: float = 0.7
    foreground_peak_quantile: float = 0.6
    foreground_minimum_spacing_seconds: float = 0.18


SETTINGS = BeatAnalysisSettings()


def _smooth(values: NDArray[np.float64], width: int) -> NDArray[np.float64]:
    """Keep one value per audio frame even when the clip is shorter than the window."""
    if not len(values):
        return values.copy()
    width = min(width, len(values))
    return np.asarray(np.convolve(values, np.ones(width) / width, mode="same"), dtype=np.float64)


def _samples(path: Path, seconds: float, loop: bool, cancel: Event | None) -> NDArray[np.float32]:
    with tempfile.TemporaryDirectory(prefix="sculptcut-beats-") as folder:
        pcm = Path(folder) / "audio.f32"
        run_process(
            [
                discover("ffmpeg"),
                "-v",
                "error",
                "-nostdin",
                *(["-stream_loop", "-1"] if loop else []),
                "-i",
                str(path),
                "-t",
                str(seconds),
                "-map",
                "0:a:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(SETTINGS.sample_rate),
                "-f",
                "f32le",
                str(pcm),
            ],
            cancel,
        )
        return np.fromfile(pcm, dtype="<f4")


def _features(
    samples: NDArray[np.float32], cancel: Event | None
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    size, hop = SETTINGS.window_size, SETTINGS.hop_size
    window = np.hanning(size)
    flux: list[float] = []
    level: list[float] = []
    previous: NDArray[np.float64] | None = None
    for start in range(0, len(samples) - size, hop):
        if cancel and start % (hop * 256) == 0 and cancel.is_set():
            raise Cancelled("Beat analysis cancelled")
        block = samples[start : start + size]
        spectrum = np.log1p(np.abs(np.fft.rfft(block * window)) / size * 200)
        band = spectrum[8:900]
        flux.append(float(np.maximum(band - previous, 0).mean()) if previous is not None else 0.0)
        level.append(float(np.sqrt(np.mean(block * block))))
        previous = band
    times = np.arange(len(flux), dtype=np.float64) * hop / SETTINGS.sample_rate + size / (
        2 * SETTINGS.sample_rate
    )
    flux_array = np.asarray(flux)
    width = max(3, round(SETTINGS.onset_baseline_seconds * SETTINGS.sample_rate / hop))
    baseline = _smooth(flux_array, width)
    onset = np.maximum(flux_array - baseline, 0)
    return times, onset, np.asarray(level)


def _spectral_attacks(
    times: NDArray[np.float64], onset: NDArray[np.float64], level: NDArray[np.float64]
) -> list[tuple[float, float]]:
    """Fallback for music without a stable tempo."""
    if len(onset) < 3 or not np.any(onset):
        return []
    threshold = max(
        float(np.quantile(onset, SETTINGS.onset_peak_quantile)), float(np.max(onset)) * 0.04
    )
    peaks = [
        i
        for i in range(1, len(onset) - 1)
        if onset[i] > threshold and onset[i] > onset[i - 1] and onset[i] >= onset[i + 1]
    ]
    reference = max(float(np.quantile(level, 0.8)), 0.0001)
    density = _onset_density(onset)
    density_reference = max(float(np.quantile(density, 0.8)), 1e-6)
    return [
        (
            float(times[i]),
            _music_activity(float(level[i]) / reference, float(density[i]) / density_reference),
        )
        for i in peaks
    ]


def _onset_density(onset: NDArray[np.float64]) -> NDArray[np.float64]:
    width = max(3, round(SETTINGS.onset_density_seconds * SETTINGS.sample_rate / SETTINGS.hop_size))
    return _smooth(onset, width)


def _music_activity(level: float, density: float) -> float:
    return min(
        1.0,
        max(0.0, (1 - SETTINGS.density_weight) * level + SETTINGS.density_weight * density),
    )


def foreground_accents(
    path: Path, seconds: float, loop: bool, cancel: Event | None
) -> list[tuple[float, float]]:
    """Find articulated midrange accents, including off-beat sung syllables.

    This is a spectral heuristic, not speech recognition. Bass and high-frequency
    percussion are downweighted so they do not mask short foreground phrases.
    """
    samples = _samples(path, seconds, loop, cancel)
    size, hop = SETTINGS.window_size, SETTINGS.hop_size
    if len(samples) < size * 3:
        return []
    bins = [
        round(hz * size / SETTINGS.sample_rate)
        for hz in (
            SETTINGS.foreground_low_hz,
            SETTINGS.foreground_mid_hz,
            SETTINGS.foreground_high_hz,
            SETTINGS.foreground_top_hz,
        )
    ]
    window = np.hanning(size)
    low_flux: list[float] = []
    mid_flux: list[float] = []
    high_flux: list[float] = []
    previous: NDArray[np.float64] | None = None
    for start in range(0, len(samples) - size, hop):
        if cancel and start % (hop * 256) == 0 and cancel.is_set():
            raise Cancelled("Foreground accent analysis cancelled")
        block = samples[start : start + size]
        spectrum = np.log1p(np.abs(np.fft.rfft(block * window)) / size * 200)
        change = (
            np.maximum(spectrum - previous, 0)
            if previous is not None
            else np.zeros_like(spectrum)
        )
        low_flux.append(float(change[bins[0] : bins[1]].mean()))
        mid_flux.append(float(change[bins[1] : bins[2]].mean()))
        high_flux.append(float(change[bins[2] : bins[3]].mean()))
        previous = spectrum
    low = np.asarray(low_flux)
    mid = np.asarray(mid_flux)
    high = np.asarray(high_flux)
    score = np.maximum(
        mid - SETTINGS.foreground_low_penalty * low - SETTINGS.foreground_high_penalty * high,
        0,
    )
    smooth_width = SETTINGS.foreground_smooth_frames
    smooth = _smooth(score, smooth_width)
    baseline_width = max(
        3, round(SETTINGS.foreground_baseline_seconds * SETTINGS.sample_rate / hop)
    )
    baseline = _smooth(smooth, baseline_width)
    novelty = np.maximum(smooth - SETTINGS.foreground_baseline_fraction * baseline, 0)
    times = (np.arange(len(novelty)) * hop + size / 2) / SETTINGS.sample_rate
    candidates = [
        index
        for index in range(1, len(novelty) - 1)
        if novelty[index] > novelty[index - 1]
        and novelty[index] >= novelty[index + 1]
        and mid[index] > low[index] * SETTINGS.foreground_mid_low_ratio
        and mid[index] > high[index] * SETTINGS.foreground_mid_high_ratio
    ]
    if not candidates:
        return []
    threshold = float(np.quantile(novelty[candidates], SETTINGS.foreground_peak_quantile))
    reference = max(float(np.quantile(novelty[candidates], 0.9)), 1e-6)
    selected: list[int] = []
    for index in sorted(candidates, key=lambda candidate: float(novelty[candidate]), reverse=True):
        if novelty[index] < threshold or any(
            abs(times[index] - times[chosen]) < SETTINGS.foreground_minimum_spacing_seconds
            for chosen in selected
        ):
            continue
        selected.append(index)
    return [
        (float(times[index]), min(1.0, float(novelty[index]) / reference))
        for index in sorted(selected)
    ]


def beat_profile(
    path: Path, seconds: float, loop: bool, cancel: Event | None
) -> list[tuple[float, float]]:
    """Return continuous tracked beats and section activity, aligned to attacks."""
    samples = _samples(path, seconds, loop, cancel)
    if len(samples) < SETTINGS.window_size * 3:
        return []
    times, onset, level = _features(samples, cancel)
    if not np.any(onset):
        return []
    grid_times = track_beats(samples, SETTINGS.sample_rate, cancel)
    if len(grid_times) < 2:
        return _spectral_attacks(times, onset, level)
    density = _onset_density(onset)
    density_reference = max(float(np.quantile(density, 0.8)), 1e-6)
    reference = max(float(np.quantile(level, 0.8)), 0.0001)
    frame_rate = SETTINGS.sample_rate / SETTINGS.hop_size
    smooth_width = max(3, round(SETTINGS.intensity_window_seconds * frame_rate))
    smooth_level = _smooth(level, smooth_width)
    beats: list[tuple[float, float]] = []
    for time in grid_times:
        local_level = float(np.interp(time, times, smooth_level))
        if local_level < reference * SETTINGS.silence_fraction:
            continue
        local_density = float(np.interp(time, times, density))
        beats.append(
            (time, _music_activity(local_level / reference, local_density / density_reference))
        )
    return beats
