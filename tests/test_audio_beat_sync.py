"""Beat sync analysis is cached and emits bounded, rhythm-aligned candidates."""

import os
import wave
from pathlib import Path

import numpy as np
import pytest

from sculptcut.audio_beat_sync import (
    MusicAnalysis,
    MusicEvent,
    analyze_music,
    select_cut_beats,
)
from sculptcut.beat_sync import apply_beat_sync, snap_cuts_to_beats
from sculptcut.domain import Project, ProjectSettings
from sculptcut.intro import intro_signature
from sculptcut.media import discover, run_process
from sculptcut.music_library import music_signature
from sculptcut.planning import RenderPlan
from sculptcut.project import create_project


def test_cut_beat_modes_change_spacing_and_respect_limits() -> None:
    analysis = MusicAnalysis(
        tempo_bpm=120,
        beats=tuple(MusicEvent(time, 0.8) for time in np.arange(0.5, 8, 0.5)),
        onsets=(),
    )
    slow = select_cut_beats(analysis, "slow", 0.4, 10)
    normal = select_cut_beats(analysis, "normal", 0.4, 10)
    fast = select_cut_beats(analysis, "fast", 0.4, 10)
    assert len(slow) < len(normal) < len(fast)
    for selected in (slow, normal, fast):
        gaps = np.diff([event.time_seconds for event in selected])
        assert np.all(gaps >= 0.4)
        assert np.all(gaps <= 10)
    bounded = select_cut_beats(analysis, "slow", 0.4, 2.5)
    assert np.all(np.diff([event.time_seconds for event in bounded]) <= 2.5)


def test_connected_offbeat_phrase_is_kept_but_single_onset_is_ignored() -> None:
    beats = tuple(MusicEvent(time, 0.55) for time in np.arange(0.5, 6, 0.5))
    onsets = tuple(
        MusicEvent(time, strength)
        for time, strength in ((2.05, 0.95), (2.47, 0.92), (2.89, 0.88), (4.82, 1.0))
    )
    selected = select_cut_beats(MusicAnalysis(120, beats, onsets), "normal", 0.4, 2.5)
    times = [event.time_seconds for event in selected]
    assert all(any(abs(time - onset) < 1e-9 for time in times) for onset in (2.05, 2.47, 2.89))
    assert 4.82 not in times


def test_snap_moves_existing_cuts_partway_and_obeys_limits() -> None:
    candidates = [MusicEvent(1.0, 0.9), MusicEvent(2.1, 1.0), MusicEvent(3.4, 0.8)]
    result = snap_cuts_to_beats([1.1, 2.2, 3.3], candidates, strength=0.75, duration=4)
    assert result == [1.025, 2.125, 3.375]
    constrained = snap_cuts_to_beats(
        [0.5], [MusicEvent(0.8, 1)], tolerance=0.35, min_interval=0.4, duration=0.9
    )
    assert constrained == [0.5]


def test_snap_does_not_move_cut_away_from_a_valid_music_event() -> None:
    result = snap_cuts_to_beats(
        [1.0],
        [MusicEvent(0.8, 1.0)],
        alignment_events=[MusicEvent(0.95, 0.8)],
    )
    assert result == [1.0]


def test_music_analysis_cache_uses_path_size_and_mtime(tmp_path: Path) -> None:
    rate = 11_025
    samples = np.zeros(rate * 4)
    for pulse in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5):
        start = round(pulse * rate)
        time = np.arange(round(0.08 * rate)) / rate
        samples[start : start + len(time)] = 0.8 * np.sin(2 * np.pi * 600 * time)
    track = tmp_path / "music.wav"
    with wave.open(str(track), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes((samples * 32767).astype("<i2").tobytes())
    cache = tmp_path / "music_analysis_cache"
    first = analyze_music(track, 4, cache)
    second = analyze_music(track, 4, cache)
    assert first == second
    assert len(list(cache.glob("*.json"))) == 1
    stat = track.stat()
    os.utime(track, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    analyze_music(track, 4, cache)
    assert len(list(cache.glob("*.json"))) == 2


def test_disabled_sync_preserves_existing_plan() -> None:
    project = Project(
        sources=[],
        settings=ProjectSettings(preset="beat_shorts", beat_sync_enabled=False),
    )
    plan = RenderPlan(
        source_fingerprints={},
        settings={"preset": "shorts"},
        output_fps=30,
        target_frame_count=30,
        decisions=[],
        actual_frame_count=30,
    )
    assert apply_beat_sync(Path("."), project, plan) is plan


def test_regular_shorts_is_never_beat_resynced() -> None:
    project = Project(
        sources=[],
        settings=ProjectSettings(preset="shorts", beat_sync_enabled=True),
    )
    plan = RenderPlan(
        source_fingerprints={},
        settings={"preset": "shorts"},
        output_fps=30,
        target_frame_count=30,
        decisions=[],
        actual_frame_count=30,
    )
    assert apply_beat_sync(Path("."), project, plan) is plan


def test_plan_sync_moves_existing_beat_shorts_cuts_and_preserves_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=160x90:r=30:d=6",
            "-c:v",
            "libx264",
            str(source),
        ]
    )
    music = tmp_path / "music.wav"
    with wave.open(str(music), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(11_025)
        output.writeframes(np.zeros(11_025 * 5, dtype="<i2").tobytes())
    project_root = tmp_path / "project"
    project = create_project(project_root, [source]).model_copy(
        update={
            "settings": ProjectSettings(
                preset="beat_shorts", target_seconds=4.5, music_path=music, music_loop=False
            )
        }
    )
    source_id = project.sources[0].id
    plan = RenderPlan(
        source_fingerprints={source_id: project.sources[0].fingerprint},
        settings={
            "preset": "beat_shorts",
            "music_fingerprint": music_signature(project.settings),
            "intro_fingerprint": intro_signature(project.settings),
        },
        output_fps=30,
        target_frame_count=135,
        actual_frame_count=135,
        decisions=[
            {
                "source_id": source_id,
                "source_start_us": index * 2_000_000,
                "source_end_us": (index + 1) * 2_000_000,
                    "action": "HOLD",
                    "output_frame_count": count,
                    "provenance": "auto:beat_still",
                    "subject_camera": {
                        "viewport": {"x": 0.1, "y": 0, "width": 0.8, "height": 1},
                        "width": 0.3,
                        "height": 0.8,
                        "points": [
                            {"time_us": index * 2_000_000, "x": 0.5, "y": 0.5}
                        ],
                        "background": 0,
                    },
            }
            for index, count in enumerate((44, 44, 47))
        ],
    )
    analysis = MusicAnalysis(
        80,
        tuple(MusicEvent(time, 0.9) for time in (0.1, 0.85, 1.6, 2.35, 3.1, 3.85)),
        (),
    )
    monkeypatch.setattr("sculptcut.beat_sync.analyze_music", lambda *args, **kwargs: analysis)

    synced = apply_beat_sync(project_root, project, plan)
    assert [item.output_frame_count for item in synced.decisions] == [48, 45, 42]
    assert 48 / 30 == 1.6  # Exact attack rather than the previous 75% time blend.
    assert synced.actual_frame_count == plan.actual_frame_count
    assert synced.target_frame_count == plan.target_frame_count
