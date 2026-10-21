"""Continuous beat tracking with local tempo evidence and measured attack times."""

from dataclasses import dataclass
from threading import Event

import numpy as np
from numpy.typing import NDArray

from sculptcut.media import Cancelled


@dataclass(frozen=True)
class BeatTrackingSettings:
    window_size: int = 512
    hop_size: int = 128
    minimum_period_seconds: float = 0.3
    maximum_period_seconds: float = 1.0
    tempo_window_seconds: float = 6.0
    tempo_step_seconds: float = 0.5
    harmonic_weights: tuple[float, ...] = (1.0, 0.5, 0.25)
    tempo_change_penalty: float = 2.0
    tempo_resolution_frames: float = 0.25
    interval_penalty: float = 65.0
    minimum_interval_ratio: float = 0.55
    maximum_interval_ratio: float = 1.8
    peak_radius_seconds: float = 0.012
    onset_baseline_seconds: float = 0.6
    minimum_attack_fraction: float = 0.15
    attack_refine_seconds: float = 0.035
    isolated_attack_refine_seconds: float = 0.09
    isolated_attack_floor_fraction: float = 0.05
    attack_energy_window_seconds: float = 0.005
    attack_energy_hop: int = 32
    attack_minimum_rise: float = 0.1


SETTINGS = BeatTrackingSettings()


def attack_envelope(
    samples: NDArray[np.float32], sample_rate: int, cancel: Event | None
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Short centered windows avoid the timing smear of a 186 ms FFT window."""
    size, hop = SETTINGS.window_size, SETTINGS.hop_size
    window = np.hanning(size)
    bands = [(40, 250), (250, 2_000), (2_000, 5_000)]
    bins = [(round(a * size / sample_rate), round(b * size / sample_rate)) for a, b in bands]
    flux: list[list[float]] = []
    previous: NDArray[np.float64] | None = None
    for start in range(0, len(samples) - size, hop):
        if cancel and start % (hop * 256) == 0 and cancel.is_set():
            raise Cancelled("Beat tracking cancelled")
        spectrum = np.log1p(np.abs(np.fft.rfft(samples[start : start + size] * window)))
        change = np.maximum(spectrum - previous, 0) if previous is not None else spectrum * 0
        flux.append([float(change[left:right].mean()) for left, right in bins])
        previous = spectrum
    values = np.asarray(flux, dtype=np.float64)
    if not len(values):
        return np.asarray([], dtype=np.float64), np.asarray([], dtype=np.float64)
    # Equalize bands: a loud vocal/bass note must not hide quieter percussion.
    scale = np.asarray(
        [
            max(float(np.quantile(band[band > 1e-6], 0.8)), 1e-6)
            if np.any(band > 1e-6)
            else 1e-6
            for band in values.T
        ]
    )
    envelope = (values / scale * np.asarray([0.45, 0.3, 0.25])).sum(axis=1)
    width = min(len(envelope), max(3, round(SETTINGS.onset_baseline_seconds * sample_rate / hop)))
    baseline = np.convolve(envelope, np.ones(width) / width, mode="same")
    envelope = np.maximum(envelope - baseline, 0)
    times = (np.arange(len(envelope), dtype=np.float64) * hop + size / 2) / sample_rate
    return times, envelope


def tempo_curve(
    times: NDArray[np.float64], onset: NDArray[np.float64], frame_rate: float
) -> NDArray[np.float64]:
    """Choose a continuous tempo path, resolving competing rhythmic multiples."""
    lags = np.arange(
        SETTINGS.minimum_period_seconds * frame_rate,
        SETTINGS.maximum_period_seconds * frame_rate,
        SETTINGS.tempo_resolution_frames,
    )
    centers = np.arange(
        times[0], times[-1] + SETTINGS.tempo_step_seconds, SETTINGS.tempo_step_seconds
    )
    evidence = []
    for center in centers:
        section = onset[np.abs(times - center) <= SETTINGS.tempo_window_seconds / 2]
        clipped = np.minimum(section, float(np.quantile(section, 0.95)))
        centered = clipped - float(clipped.mean())
        norm = max(float(np.dot(centered, centered)), 1e-12)
        max_lag = min(len(centered) - 1, int(lags[-1]) * len(SETTINGS.harmonic_weights))
        correlation = np.asarray(
            [0.0]
            + [
                float(np.dot(centered[:-lag], centered[lag:])) / norm
                for lag in range(1, max_lag + 1)
            ]
        )
        score = np.zeros(len(lags))
        for harmonic, weight in enumerate(SETTINGS.harmonic_weights, start=1):
            score += weight * np.interp(
                lags * harmonic, np.arange(len(correlation)), correlation, right=0
            )
        evidence.append(score)
    scores = np.asarray(evidence)
    # Accumulating section evidence permits real tempo changes while rejecting
    # a single window's spurious 2x/3x tempo interpretation.
    transition = SETTINGS.tempo_change_penalty * np.abs(np.log(lags[:, None] / lags[None, :]))
    best = scores[0].copy()
    previous_rows = []
    for row in scores[1:]:
        choices = best[:, None] - transition
        previous = np.argmax(choices, axis=0)
        best = row + choices[previous, np.arange(len(lags))]
        previous_rows.append(previous)
    index = int(np.argmax(best))
    path = [index]
    for previous in reversed(previous_rows):
        index = int(previous[index])
        path.append(index)
    periods = lags[np.asarray(path[::-1])] / frame_rate
    return np.asarray(np.interp(times, centers, periods), dtype=np.float64)


def track_beats(
    samples: NDArray[np.float32], sample_rate: int, cancel: Event | None
) -> list[float]:
    """Track one beat path across sections; cuts use observed attacks, not FFT blocks."""
    times, onset = attack_envelope(samples, sample_rate, cancel)
    if len(times) < 3 or not np.any(onset):
        return []
    rate = sample_rate / SETTINGS.hop_size
    periods = tempo_curve(times, onset, rate)
    reference = max(float(np.quantile(onset[onset > 0], 0.9)), 1e-6)
    salience = np.minimum(onset / reference, 1.5)
    # Dynamic programming carries phase continuously through every section.
    # Weak/missing attacks can bridge the path without resetting its phase.
    score = np.zeros(len(times))
    previous = np.full(len(times), -1, dtype=np.int64)
    for index in range(1, len(times)):
        if cancel and index % 256 == 0 and cancel.is_set():
            raise Cancelled("Beat tracking cancelled")
        period = periods[index] * rate
        lookback = max(
            0,
            index - round(SETTINGS.maximum_period_seconds * rate * SETTINGS.maximum_interval_ratio),
        )
        largest_period = float(np.max(periods[lookback : index + 1])) * rate
        left = max(0, index - round(largest_period * SETTINGS.maximum_interval_ratio))
        right = max(0, index - round(period * SETTINGS.minimum_interval_ratio))
        if right <= left:
            score[index] = salience[index]
            continue
        candidates = np.arange(left, right)
        expected = np.maximum(periods[candidates] * rate, period)
        penalty = SETTINGS.interval_penalty * np.log((index - candidates) / expected) ** 2
        values = score[candidates] - penalty
        best = int(np.argmax(values))
        if values[best] > 0:
            previous[index] = candidates[best]
            score[index] = salience[index] + values[best]
        else:
            score[index] = salience[index]
    end = int(np.argmax(score))
    path = []
    while end >= 0:
        path.append(end)
        end = int(previous[end])
    radius = max(1, round(SETTINGS.peak_radius_seconds * rate))
    beats: list[float] = []
    for index in reversed(path):
        left, right = max(0, index - radius), min(len(times), index + radius + 1)
        peak = left + int(np.argmax(onset[left:right]))
        if onset[peak] < reference * SETTINGS.minimum_attack_fraction:
            continue  # Do not create a cut in silence just to complete the grid.
        time = float(times[peak])
        if not beats or time > beats[-1]:
            beats.append(time)
    return refine_attacks(samples, sample_rate, beats)


def refine_attacks(
    samples: NDArray[np.float32], sample_rate: int, beats: list[float]
) -> list[float]:
    """Locate a sharp energy rise at sample scale; sustained tones keep spectral timing."""
    width = max(1, round(SETTINGS.attack_energy_window_seconds * sample_rate))
    starts = np.arange(0, len(samples) - width, SETTINGS.attack_energy_hop)
    energy = np.r_[0.0, np.cumsum(samples.astype(np.float64) ** 2)]
    rms = np.sqrt((energy[starts + width] - energy[starts]) / width)
    times = (starts + width / 2) / sample_rate
    rise = np.r_[0.0, np.maximum(np.diff(rms), 0)]
    refined: list[float] = []
    for beat in beats:
        left = int(np.searchsorted(times, beat - SETTINGS.attack_refine_seconds))
        right = int(np.searchsorted(times, beat + SETTINGS.attack_refine_seconds))
        if right <= left:
            continue
        local_level = max(float(np.max(rms[left:right])), 1e-6)
        if (
            float(np.min(rms[left:right])) < local_level * SETTINGS.isolated_attack_floor_fraction
            and float(np.max(rise[left:right])) < local_level * SETTINGS.attack_minimum_rise
        ):
            left = int(np.searchsorted(times, beat - SETTINGS.isolated_attack_refine_seconds))
            right = int(np.searchsorted(times, beat + SETTINGS.isolated_attack_refine_seconds))
        peak = left + int(np.argmax(rise[left:right]))
        time = (
            float(times[peak]) if rise[peak] >= local_level * SETTINGS.attack_minimum_rise else beat
        )
        if not refined or time > refined[-1]:
            refined.append(time)
    return refined
