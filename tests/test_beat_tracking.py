"""Known audio attacks, changing tempo, and beat-counted edit cadence."""

import wave
from pathlib import Path
from threading import Event

import numpy as np
import pytest

from sculptcut.audio_beat_sync import MusicAnalysis, MusicEvent, select_cut_beats
from sculptcut.beat_shorts import beat_boundaries
from sculptcut.domain import Project, ProjectSettings
from sculptcut.media import Cancelled
from sculptcut.music_beats import beat_profile


def write_rhythm(path: Path, beats: np.ndarray, seconds: int, *, distractors: bool = False) -> None:
    rate = 11_025
    samples = np.zeros(rate * seconds)
    for index, beat in enumerate(beats):
        start = round(float(beat) * rate)
        time = np.arange(round(0.07 * rate)) / rate
        volume = 0.7 if index % 4 == 0 else 0.15
        samples[start : start + len(time)] += (
            volume
            * np.exp(-40 * time)
            * (np.sin(2 * np.pi * 95 * time) + 0.4 * np.sin(2 * np.pi * 1600 * time))
        )
    if distractors:
        for beat in (2.3, 7.13, 9.3, 11.86):
            start = round(beat * rate)
            time = np.arange(round(0.09 * rate)) / rate
            samples[start : start + len(time)] += (
                0.9 * np.exp(-15 * time) * np.sin(2 * np.pi * 700 * time)
            )
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())


@pytest.mark.parametrize("pattern", ["steady", "step", "ramp", "distractors"])
def test_attacks_follow_real_tempo_changes_to_one_output_frame(
    tmp_path: Path, pattern: str
) -> None:
    if pattern == "step":
        # Change at 5.37 seconds, away from the old 8-second section boundaries.
        expected = np.r_[np.arange(0.37, 5.2, 0.7), np.arange(5.37, 16, 0.41)]
    elif pattern == "ramp":
        expected = np.cumsum(np.linspace(0.65, 0.36, 36)) + 0.11
    else:
        expected = np.arange(0.37, 16, 0.57)
    track = tmp_path / f"{pattern}.wav"
    write_rhythm(track, expected, 20, distractors=pattern == "distractors")
    measured = [time for time, _ in beat_profile(track, 20, False, None)]
    # Reference is the generated sample attack, independent of the analyzer's grid.
    assert len(measured) == len(expected)
    for detected, attack in zip(measured, expected, strict=True):
        assert abs(round(detected * 30) / 30 - float(attack)) <= 1 / 30
    project = Project(
        sources=[],
        settings=ProjectSettings(
            preset="beat_shorts",
            music_path=track,
            music_loop=False,
            beat_sync_mode="fast",
            beat_sync_min_shot_seconds=0.3,
        ),
    )
    cuts = beat_boundaries(tmp_path, project, 600, None)[1:-1]
    assert cuts
    for frame in cuts:
        assert min(abs(frame / 30 - float(attack)) for attack in expected) <= 1 / 30


def test_mode_counts_beats_despite_jitter_and_changing_tempo() -> None:
    times = np.r_[np.arange(0.3, 6, 0.71), np.arange(6.4, 12, 0.41)]
    times += np.where(np.arange(len(times)) % 2, 0.012, -0.012)
    analysis = MusicAnalysis(100, tuple(MusicEvent(float(t), 0.9) for t in times), ())
    for mode, step in (("slow", 4), ("normal", 2), ("fast", 1)):
        selected = select_cut_beats(analysis, mode, 0.3, 10)
        assert [event.time_seconds for event in selected] == list(times[::step])


def test_irregular_connected_foreground_does_not_hijack_pulse() -> None:
    beats = tuple(MusicEvent(float(t), 0.9) for t in np.arange(0.5, 8, 0.5))
    irregular = tuple(MusicEvent(t, 1.0) for t in (2.08, 2.27, 2.76, 3.09))
    selected = select_cut_beats(MusicAnalysis(120, beats, irregular))
    assert [event.time_seconds for event in selected] == [
        event.time_seconds for event in beats[::2]
    ]


def test_music_analysis_remains_cancellable(tmp_path: Path) -> None:
    track = tmp_path / "cancel.wav"
    write_rhythm(track, np.arange(0.37, 16, 0.57), 20)
    cancel = Event()
    cancel.set()
    with pytest.raises(Cancelled):
        beat_profile(track, 20, False, cancel)
