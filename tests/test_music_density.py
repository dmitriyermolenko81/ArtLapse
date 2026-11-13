"""Adaptive musical cut density and safe visual emphasis on isolated strong beats."""

import wave
from pathlib import Path

import numpy as np
import pytest
from test_timeline import make_source

from sculptcut.analysis import FrameFeature
from sculptcut.audio_beat_sync import (
    MusicAnalysis,
    MusicEvent,
    analyze_music,
    section_activity,
    select_cut_beats,
)
from sculptcut.beat_shorts import Candidate, _emphasize_accent_views, beat_grid
from sculptcut.domain import CameraPoint, Project, ProjectSettings, Roi, SubjectCamera
from sculptcut.music_episodes import episode_grid


def activity_track(path: Path) -> Path:
    rate = 11_025
    samples = np.zeros(rate * 16)
    for beat in np.arange(.5, 16, .5):
        time = np.arange(round(.09 * rate)) / rate
        strength = .12 if beat < 8 else .8
        if beat == 5:
            strength = 1.0
        pulse = strength * np.exp(-25 * time) * np.sin(2 * np.pi * 600 * time)
        start = round(beat * rate)
        samples[start:start + len(time)] += pulse
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes((samples * 32767).astype("<i2").tobytes())
    return path


@pytest.mark.parametrize("mode", ["slow", "normal", "fast"])
def test_quiet_normal_energetic_sections_and_isolated_accent(mode: str) -> None:
    beats = tuple(MusicEvent(float(t), 1 if t == 2 else .2 if t < 8 else .65 if t < 16 else 1,
                             1 if t == 2 else 0) for t in np.arange(.5, 24, .5))
    analysis = MusicAnalysis(120, beats, ())
    events = select_cut_beats(analysis, mode, .4, 2.5)
    times = [e.time_seconds for e in events]
    quiet = sum(3 <= t < 7 for t in times)
    normal = sum(10 <= t < 14 for t in times)
    busy = sum(18 <= t < 22 for t in times)
    assert quiet < busy and quiet <= normal <= busy
    if mode != "fast":
        assert normal < busy
    assert 2 in times  # A strong beat survives the quiet section's skipped beats.
    assert all(e in beats for e in events)
    assert all(.4 <= gap <= 2.5 for gap in np.diff(times))
    assert max(section_activity(beats)[:12]) < .45


@pytest.mark.parametrize("preset", [
    "beat_shorts", "music_shorts", "photoshop_beat_shorts", "photoshop_music_shorts",
])
def test_real_music_density_accent_cache_and_bookend_clock(tmp_path: Path, preset: str) -> None:
    track = activity_track(tmp_path / "activity.wav")
    analysis = analyze_music(track, 16, tmp_path / "cache")
    assert analyze_music(track, 16, tmp_path / "cache") == analysis
    quiet = [e.strength for e in analysis.beats if 1 < e.time_seconds < 4]
    busy = [e.strength for e in analysis.beats if 10 < e.time_seconds < 14]
    assert np.mean(busy) > np.mean(quiet) * 2
    accent = min(analysis.beats, key=lambda e: abs(e.time_seconds - 5))
    assert abs(accent.time_seconds - 5) < .09 and accent.accent_strength >= .8
    events = select_cut_beats(analysis, "normal", .4, 2.5)
    assert accent in events
    assert sum(9 < e.time_seconds < 14 for e in events) > sum(
        1 < e.time_seconds < 4 for e in events
    )
    project = Project(sources=[], settings=ProjectSettings.model_validate({
        "preset": preset, "music_path": track, "presentation_video": track,
        "presentation_seconds": 1.5, "intro_image": tmp_path / "not-decoded.png",
        "intro_position": "end", "intro_seconds": 1, "intro_transition_seconds": .5,
    }))
    # The grid is on the body clock, not on the presentation's clock.
    cuts, accents = beat_grid(tmp_path, project, 420, None)
    target = round(accent.time_seconds * 30) - 45
    assert cuts[0] == 0 and cuts[-1] == 420
    assert target in cuts and target in accents
    assert target in episode_grid(project, 420, events, 1.5)


def test_visual_accent_prefers_distinct_vetted_view_without_reordering() -> None:
    source = make_source(0, 0, 10_000_000)
    camera = SubjectCamera(viewport=Roi(x=0, y=0, width=1, height=1), width=1, height=1,
                           points=[CameraPoint(time_us=0, x=.5, y=.5)], background=32)

    def candidate(seconds: float) -> Candidate:
        fields = {name: 0 for name in FrameFeature.model_fields}
        feature = FrameFeature.model_validate(fields | {
            "source_id": source.id, "timestamp_us": round(seconds * 1e6),
            "perceptual_hash": "0", "measurement_quality": 1,
        })
        return Candidate(source, feature, 1)

    before, weak, alternative, after = [candidate(t) for t in (1, 3, 4, 6)]
    unsafe, future = candidate(4.5), candidate(7)
    all_candidates = [before, weak, alternative, after, unsafe, future]
    images = [20, 22, 50, 40, 100, 120]
    usable = {(c.source.id, c.feature.timestamp_us): (1 if c is not unsafe else .1, c, camera)
              for c in all_candidates}
    appearance = {(c.source.id, c.feature.timestamp_us): np.full((80, 48), value, np.uint8)
                  for c, value in zip(all_candidates, images, strict=True)}
    selected = [(0, before, camera), (1, weak, camera), (2, after, camera)]
    choices = [[before], [weak, alternative, unsafe, future], [after]]
    assert _emphasize_accent_views(selected, choices, usable, appearance, [0, 30, 60, 90],
                                   set()) == selected
    result = _emphasize_accent_views(selected, choices, usable, appearance, [0, 30, 60, 90], {30})
    assert [c.feature.timestamp_us for _, c, _ in result] == [1_000_000, 4_000_000, 6_000_000]
    assert [slot for slot, _, _ in result] == [0, 1, 2]
    assert selected[1][1] is weak
    ordinary_cut = _emphasize_accent_views(selected, choices, usable, appearance,
                                          [0, 30, 60, 90], set(), all_cuts=True)
    assert ordinary_cut == result
    # Improving this cut must not turn the next one into another near duplicate.
    appearance[(alternative.source.id, alternative.feature.timestamp_us)].fill(47)
    assert _emphasize_accent_views(selected, choices, usable, appearance, [0, 30, 60, 90],
                                   set(), all_cuts=True) == selected
    # Never substitute a poor, out-of-order or visually repeated following view.
    appearance[(alternative.source.id, alternative.feature.timestamp_us)].fill(40)
    assert _emphasize_accent_views(selected, choices, usable, appearance, [0, 30, 60, 90],
                                   {30}) == selected
