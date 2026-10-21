"""Spectral beats must remain visible when the overall loudness stays steady."""

import wave
from pathlib import Path

import numpy as np
import pytest

from sculptcut.beat_shorts import _phrase_accents, beat_boundaries
from sculptcut.domain import Project, ProjectSettings
from sculptcut.music_beats import beat_profile, foreground_accents


def test_spectral_beats_follow_fixed_level_frequency_changes(tmp_path: Path) -> None:
    rate = 11_025
    block = rate // 2
    seconds = 10
    samples = np.concatenate(
        [
            0.2 * np.sin(2 * np.pi * frequency * np.arange(block) / rate)
            for frequency in ([330, 880] * seconds)
        ]
    )
    track = tmp_path / "same-loudness.wav"
    with wave.open(str(track), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes((samples * 32767).astype("<i2").tobytes())
    beats = [time for time, _ in beat_profile(track, seconds, False, None)]
    assert len(beats) >= 15
    for expected in np.arange(1, 9.5, 0.5):
        assert min(abs(time - expected) for time in beats) < 0.09


def test_music_grid_follows_section_tempo_and_onset_density(tmp_path: Path) -> None:
    rate = 11_025
    seconds = 16
    samples = np.zeros(rate * seconds, dtype=np.float64)
    beat_times = [*np.arange(0.5, 8, 0.75), *np.arange(8.2, seconds, 0.4)]
    for beat in beat_times:
        start = round(float(beat) * rate)
        count = round(0.07 * rate)
        time = np.arange(count) / rate
        samples[start : start + count] = 0.7 * np.sin(2 * np.pi * 600 * time)
    track = tmp_path / "tempo-and-density.wav"
    with wave.open(str(track), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes((samples * 32767).astype("<i2").tobytes())

    profile = beat_profile(track, seconds, False, None)
    slow = [(time, activity) for time, activity in profile if 1 < time < 7.5]
    fast = [(time, activity) for time, activity in profile if 9 < time < 15.5]
    slow_period = np.diff([time for time, _ in slow]).mean()
    fast_period = np.diff([time for time, _ in fast]).mean()
    assert slow_period > fast_period * 1.5
    assert np.mean([activity for _, activity in fast]) > np.mean(
        [activity for _, activity in slow]
    )


@pytest.mark.parametrize("voice_hz", [440, 900])
def test_offbeat_phrase_accents_take_priority_over_drums(tmp_path: Path, voice_hz: int) -> None:
    rate = 11_025
    seconds = 6
    samples = np.zeros(rate * seconds, dtype=np.float64)
    for beat in np.arange(0.3, 5.8, 0.6):
        start = round(float(beat) * rate)
        time = np.arange(round(0.08 * rate)) / rate
        samples[start : start + len(time)] += 0.7 * np.exp(-35 * time) * np.sin(
            2 * np.pi * 90 * time
        )
    drums = tmp_path / f"drums-{voice_hz}.wav"
    with wave.open(str(drums), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    assert foreground_accents(drums, seconds, False, None) == []
    phrase = (2.05, 2.33, 2.61)
    for syllable in phrase:
        start = round(syllable * rate)
        time = np.arange(round(0.16 * rate)) / rate
        envelope = np.sin(np.pi * np.arange(len(time)) / len(time))
        samples[start : start + len(time)] += 0.45 * envelope * (
            np.sin(2 * np.pi * voice_hz * time)
            + 0.3 * np.sin(2 * np.pi * voice_hz * 2 * time)
        )
    track = tmp_path / f"offbeat-{voice_hz}.wav"
    with wave.open(str(track), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())

    accents = [time for time, _ in foreground_accents(track, seconds, False, None)]
    project = Project(
        sources=[], settings=ProjectSettings(preset="beat_shorts", music_path=track)
    )
    cuts = [
        frame / project.settings.output_fps
        for frame in beat_boundaries(tmp_path, project, 180, None)
    ]
    for syllable in phrase:
        assert min(abs(time - syllable) for time in accents) < 0.09
        assert min(abs(time - syllable) for time in cuts) < 0.09


def test_isolated_foreground_attack_does_not_replace_beat() -> None:
    accents = [
        (0.4, 0.9),
        (2.05, 1.0),
        (2.33, 0.95),
        (2.61, 0.9),
        (4.82, 1.0),
    ]
    assert _phrase_accents(accents) == accents[1:4]
