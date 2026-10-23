from pathlib import Path
from threading import Event

import cv2
import numpy as np
import pytest
from test_beat_shorts import pulse_track
from test_timeline import make_source

from sculptcut.audio_beat_sync import MusicEvent
from sculptcut.domain import Override, Project, ProjectSettings, Roi
from sculptcut.media import Cancelled, discover, probe, run_process
from sculptcut.music_episodes import episode_grid, retime_episodes
from sculptcut.overrides import apply_overrides, exclude_dropped
from sculptcut.photoshop_music import photoshop_music_plan
from sculptcut.planning import build_plan, plan_project, validate_plan
from sculptcut.project import create_project, fingerprint, load_project, save_project
from sculptcut.rendering import render, verify_output


@pytest.mark.parametrize("intro_offset", [0.0, 1.5])
def test_music_grid_selects_readable_chronological_drawing_clips(intro_offset: float) -> None:
    project = Project(sources=[make_source(0, 0, 3_600_000_000)], settings=ProjectSettings(
        preset="photoshop_music_shorts",
    ), overrides=[Override(source_id="0", start_us=100_000_000, end_us=300_000_000,
                           action="DROP")])
    base = exclude_dropped(project, build_plan(project, [], 120))
    events = [MusicEvent(i * .6 + intro_offset, 1) for i in range(1, 200)]
    grid = episode_grid(project, 3600, events, intro_offset)
    selected = photoshop_music_plan(project, base, grid, lambda *args: 0)
    result = retime_episodes(project, apply_overrides(project, selected), events, intro_offset)
    validate_plan(project, result)
    shots = [d for d in result.decisions if d.output_frame_count]
    assert len(shots) == len(grid) - 1 > 45
    assert result.actual_frame_count == 3600
    assert result.settings["story"] == "photoshop_music_episodes"
    assert result.settings["beat_sync_aligned_cuts"] == len(shots) - 1
    assert shots[-1].source_end_us == project.sources[-1].duration_us
    for shot in shots:
        assert shot.action == "SPEED"
        assert shot.source_end_us <= 100_000_000 or shot.source_start_us >= 300_000_000
        assert shot.output_frame_count * 1_000_000 <= (
            shot.source_end_us - shot.source_start_us
        ) * 30 <= shot.output_frame_count * 16_000_000
    shifted = retime_episodes(project, selected, [MusicEvent(e.time_seconds + .2, 1)
                                                for e in events], intro_offset)
    assert shifted.decisions != result.decisions
    assert shifted.settings["beat_sync_aligned_cuts"] == len(shots) - 1


def test_drawing_music_scarce_footage_silence_locks_and_cancellation() -> None:
    project = Project(sources=[make_source(0, 0, 2_000_000), make_source(1, 2_000_000, 3_000_000)],
                      settings=ProjectSettings(preset="photoshop_music_shorts"))
    base = build_plan(project, [], 20)
    selected = photoshop_music_plan(project, base, [], lambda *args: 0)
    validate_plan(project, selected)
    assert selected.actual_frame_count <= 150
    assert selected.warnings
    quiet = retime_episodes(project, selected, [])
    assert quiet.decisions == selected.decisions
    assert quiet.settings["beat_sync"] == "unavailable"
    first = next(d for d in selected.decisions if d.output_frame_count)
    locked = first.model_copy(update={"provenance": "user:SET_SPEED"})
    selected.decisions[selected.decisions.index(first)] = locked
    synced = retime_episodes(project, selected, [MusicEvent(.7, 1), MusicEvent(2.1, 1)])
    assert locked in synced.decisions
    cancel = Event()
    cancel.set()
    with pytest.raises(Cancelled):
        photoshop_music_plan(project, base, [], lambda *args: 0, cancel)


def test_long_music_phrases_keep_moving_footage_for_the_whole_slot() -> None:
    project = Project(sources=[make_source(0, 0, 3_600_000_000)], settings=ProjectSettings(
        preset="photoshop_music_shorts",
    ))
    base = build_plan(project, [], 90)
    grid = [0, 600, 1500, 2700]
    selected = photoshop_music_plan(project, base, grid, lambda *args: 0)
    result = retime_episodes(project, selected, [MusicEvent(20, 1), MusicEvent(50, 1)])
    assert result.actual_frame_count == 2700
    assert [d.output_frame_count for d in result.decisions if d.output_frame_count] == [
        600, 900, 1200,
    ]
    assert result.settings["beat_sync_aligned_cuts"] == 2


def test_fractional_last_music_episode_tracking_has_the_export_frame_count(tmp_path: Path) -> None:
    from sculptcut.subject_camera import tracking_clip

    video = tmp_path / "drawing.mp4"
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 "testsrc2=s=160x90:r=15:d=16", "-c:v", "libx264", str(video)])
    source = create_project(tmp_path / "project", [video]).sources[0]
    clip = tracking_clip(source, 14_083_333, 16_000_000, tmp_path / "tracking.mp4", None, 23, 30)
    verify_output(clip, 23, 30)


@pytest.mark.parametrize("layout", ["auto", "square"])
def test_drawing_music_real_video_audio_preview_and_remusic(tmp_path: Path, layout: str) -> None:
    video = tmp_path / "drawing.mp4"
    run_process([
        discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
        "color=c=0x222222:s=320x180:r=15:d=16,"
        "drawbox=x=48:y=12:w=224:h=156:color=0xe0c080:t=fill,"
        "drawbox=x=125:y=35:w=70:h=105:color=0x553366:t=fill",
        "-f", "lavfi", "-i", "color=c=white:s=10x10:r=15:d=16",
        "-filter_complex", "[0:v][1:v]overlay=x='150+20*sin(t*4)':y=80:shortest=1",
        "-c:v", "libx264", str(video),
    ])
    before = fingerprint(video)
    music = tmp_path / "pulse.wav"
    pulse_track(music, [.25 + i * .5 for i in range(16)], seconds=8)
    root = tmp_path / "project"
    created = create_project(root, [video])
    sid = created.sources[0].id
    settings = ProjectSettings(
        preset="photoshop_music_shorts", music_path=music, target_seconds=6,
        shorts_layout=layout, music_fade_seconds=0,
        shorts_square_rois={sid: Roi(x=.25, y=.05, width=.5, height=.9)},
    )
    project = created.model_copy(update={
        "settings": settings,
        "rois": {sid: Roi(x=.15, y=1/15, width=.7, height=13/15)},
        "overrides": [Override(source_id=sid, start_us=4_000_000, end_us=5_000_000,
                               action="DROP")],
    })
    save_project(root, project)
    assert load_project(root) == project
    plan = plan_project(root, project, 6)
    validate_plan(project, plan)
    assert plan_project(root, project, 6) == plan
    shots = [d for d in plan.decisions if d.output_frame_count]
    assert len(shots) >= 4
    assert all(d.action == "SPEED" for d in shots)
    assert all(d.source_end_us <= 4_000_000 or d.source_start_us >= 5_000_000 for d in shots)
    assert all(d.subject_camera is not None for d in shots)
    assert all(len(d.subject_camera.points) == 1 for d in shots if d.subject_camera is not None)
    assert plan.settings["beat_sync_aligned_cuts"] == len(shots) - 1
    assert plan.actual_frame_count == 180
    # No ZBrush feature extraction or its full proxy cache is needed.
    assert not list((root / "proxy").iterdir())
    for preview in (False, True):
        output = render(root, project, plan, tmp_path / f"episodes-{preview}.mp4",
                        preview=preview, encoder="libx264")
        verify_output(output, 180, 30, expected_audio=True)
        metadata = probe(output)
        assert (metadata.width, metadata.height) == ((360, 640) if preview else (1080, 1920))
        capture = cv2.VideoCapture(str(output))
        try:
            ok, a = capture.read()
            capture.set(cv2.CAP_PROP_POS_FRAMES, shots[0].output_frame_count - 1)
            ok_b, b = capture.read()
            assert ok and ok_b
            assert np.mean(cv2.absdiff(a, b)) > .05  # Motion inside an episode, not a HOLD.
        finally:
            capture.release()
    if layout == "auto":
        from sculptcut.cli import main
        from sculptcut.output_naming import output_suffix
        from sculptcut.remusic import replace_music
        from sculptcut.variants import read_variant

        assert output_suffix(settings.preset) == "_photoshop_music_shorts"
        assert main(["plan", str(root), "--preset", "photoshop_music_shorts",
                     "--target-seconds", "6"]) == 0
        folder = tmp_path / "other-music"
        folder.mkdir()
        pulse_track(folder / "other.wav", [.45 + i * .7 for i in range(11)], seconds=8)
        output = tmp_path / "episodes-False.mp4"
        changed = replace_music(root, output, folder, 17, None)
        state = read_variant(changed)
        changed_plan = type(plan).model_validate(state["plan"])
        assert state["project"]["settings"]["preset"] == "photoshop_music_shorts"
        assert changed_plan.decisions != plan.decisions
        assert changed_plan.settings["beat_sync_aligned_cuts"] == (
            changed_plan.settings["beat_sync_total_cuts"]
        )
        verify_output(changed, 180, 30, expected_audio=True)
    assert fingerprint(video) == before
