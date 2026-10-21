from pathlib import Path
from threading import Event

import pytest
from test_timeline import make_source

from sculptcut.audio_beat_sync import MusicEvent
from sculptcut.domain import Project, ProjectSettings
from sculptcut.media import Cancelled
from sculptcut.music_episodes import episode_grid, retime_episodes
from sculptcut.planning import RenderDecision, RenderPlan, build_plan, validate_plan


def motion_plan() -> tuple[Project, RenderPlan]:
    project = Project(sources=[make_source(0, 0, 42_000_000)], settings=ProjectSettings(
        preset="music_shorts", final_view_seconds=0,
    ))
    decisions = []
    for i in range(6):
        start = i * 7_000_000
        decisions.append(RenderDecision(
            source_id="0", source_start_us=start, source_end_us=start + 6_000_000,
            action="SPEED", output_frame_count=60, provenance="user:process_story",
        ))
        decisions.append(RenderDecision(
            source_id="0", source_start_us=start + 6_000_000, source_end_us=start + 7_000_000,
            action="DROP", output_frame_count=0, provenance="user:DROP",
        ))
    return project, build_plan(project, [], 12).model_copy(update={"decisions": decisions})


@pytest.mark.parametrize("intro_offset", [0.0, 1.5])
def test_moving_episodes_follow_accents_without_source_edits(intro_offset: float) -> None:
    project, plan = motion_plan()
    cuts = [1.3, 3.7, 5.2, 7.6, 10.1]
    result = retime_episodes(project, plan, [MusicEvent(t + intro_offset, 1) for t in cuts],
                             intro_offset)
    validate_plan(project, result)
    assert result.actual_frame_count == result.target_frame_count == 360
    position = 0
    actual = []
    for before, after in zip(plan.decisions, result.decisions, strict=True):
        assert before.model_copy(update={"output_frame_count": after.output_frame_count}) == after
        if after.output_frame_count:
            assert after.action == "SPEED"
            position += after.output_frame_count
            actual.append(position / 30)
            assert (after.source_end_us - after.source_start_us) * 30 <= (
                after.output_frame_count * 8_000_000
            )
    assert actual[:-1] == cuts
    assert result.settings["beat_sync_aligned_cuts"] == len(cuts)


def test_music_retiming_preserves_speed_locks_silence_and_cancel() -> None:
    project, plan = motion_plan()
    locked = plan.decisions[2].model_copy(update={"provenance": "user:SET_SPEED"})
    plan.decisions[2] = locked
    synced = retime_episodes(project, plan, [MusicEvent(t, 1) for t in (1.3, 3.7, 5.2, 7.6)])
    assert synced.decisions[2] == locked
    validate_plan(project, synced)
    quiet = retime_episodes(project, plan, [])
    assert quiet.decisions == plan.decisions
    assert quiet.settings["beat_sync"] == "unavailable"
    cancel = Event()
    cancel.set()
    with pytest.raises(Cancelled):
        retime_episodes(project, plan, [], cancel=cancel)


def test_fast_music_grid_selects_more_than_45_readable_episodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sculptcut.development import DevelopmentEvidence
    from sculptcut.process_story import process_plan
    from sculptcut.segmentation import Segment

    project = Project(sources=[make_source(0, 0, 3_600_000_000)], settings=ProjectSettings(
        preset="music_shorts", final_view_seconds=0,
    ))
    events = [MusicEvent(i * 0.6, 1) for i in range(1, 200)]
    grid = episode_grid(project, 3600, events)
    monkeypatch.setattr("sculptcut.music_episodes.music_episode_grid", lambda *args: grid)
    monkeypatch.setattr("sculptcut.process_story.project_screens", lambda *args: [])
    monkeypatch.setattr("sculptcut.sculpt_only.final_start", lambda *args: None)
    monkeypatch.setattr("sculptcut.shorts_quality.EpisodeInspector.score", lambda *args: 1.0)
    monkeypatch.setattr("sculptcut.shorts_quality.EpisodeInspector.signature", lambda *args: None)
    monkeypatch.setattr("sculptcut.shorts_quality.EpisodeInspector.development",
                        lambda *args: DevelopmentEvidence())
    segments = [Segment(source_id="0", start_us=0, end_us=3_600_000_000, kind="SCULPT", score=1)]
    plan = process_plan(tmp_path, project, build_plan(project, [], 120), segments)
    result = retime_episodes(project, plan, events)
    active = [d for d in result.decisions if d.output_frame_count]
    assert len(active) == len(grid) - 1 > 45
    assert result.actual_frame_count == 3600
    assert result.settings["beat_sync_aligned_cuts"] == len(active) - 1
    assert all(d.action == "SPEED" for d in active)
    assert all((d.source_end_us - d.source_start_us) * 30
               <= d.output_frame_count * 8_000_000 for d in active)
    # A phase shift must change every unconstrained cut, not merely a few nearby cuts.
    shifted = retime_episodes(project, plan, [MusicEvent(e.time_seconds + .2, 1) for e in events])
    assert shifted.settings["beat_sync_aligned_cuts"] == shifted.settings["beat_sync_total_cuts"]
    assert shifted.decisions != result.decisions


def test_music_episode_plan_real_video_export_and_preview(tmp_path: Path) -> None:
    import cv2
    import numpy as np
    from test_beat_shorts import pulse_track

    from sculptcut.analysis import extract_features
    from sculptcut.domain import Override, Roi
    from sculptcut.fixtures import generate_fixture
    from sculptcut.media import discover, run_process
    from sculptcut.planning import plan_project
    from sculptcut.project import create_project, save_project
    from sculptcut.rendering import render, verify_output

    sample = generate_fixture(tmp_path / "sample.mp4", "local", duration=3, fps=10)
    video = tmp_path / "work.mp4"
    run_process([discover("ffmpeg"), "-v", "error", "-stream_loop", "11", "-i", str(sample),
                 "-c", "copy", str(video)])
    music = tmp_path / "pulse.wav"
    pulse_track(music, [0.3 + i * 0.5 for i in range(30)], seconds=16)
    root = tmp_path / "project"
    created = create_project(root, [video])
    sid = created.sources[0].id
    settings = ProjectSettings(
        preset="music_shorts", target_seconds=12, music_path=music, proxy_width=192,
        proxy_fps=5, sculpt_only=True, final_view_seconds=0, music_fade_seconds=0,
    )
    project = created.model_copy(update={
        "settings": settings,
        "overrides": [Override(source_id=sid, start_us=12_000_000,
                               end_us=14_000_000, action="DROP")],
    })
    save_project(root, project)
    extract_features(root, project)
    plan = plan_project(root, project, 12)
    validate_plan(project, plan)
    active = [d for d in plan.decisions if d.output_frame_count]
    assert len(active) >= 3
    assert all(d.action in ("SPEED", "KEEP") for d in active)
    assert all(d.source_end_us <= 12_000_000 or d.source_start_us >= 14_000_000 for d in active)
    assert plan.settings["story"] == "music_episodes"
    assert plan.settings["beat_sync_aligned_cuts"] == len(active) - 1
    assert plan_project(root, project, 12) == plan
    output = render(root, project, plan, tmp_path / "music-episodes.mp4", encoder="libx264")
    verify_output(output, plan.actual_frame_count, plan.output_fps, expected_audio=True)
    run_process([discover("ffmpeg"), "-v", "error", "-xerror", "-i", str(output),
                 "-f", "null", "-"])
    capture = cv2.VideoCapture(str(output))
    try:
        assert capture.get(cv2.CAP_PROP_FRAME_WIDTH) == 1080
        assert capture.get(cv2.CAP_PROP_FRAME_HEIGHT) == 1920
        capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
        ok_a, a = capture.read()
        capture.set(cv2.CAP_PROP_POS_FRAMES, max(1, active[0].output_frame_count - 1))
        ok_b, b = capture.read()
        assert ok_a and ok_b and np.mean(cv2.absdiff(a, b)) > 0.01
    finally:
        capture.release()
    # Same motion plan also works with the other existing composition and proxy preview.
    from sculptcut.shorts_layout import apply_square_layout

    square = project.model_copy(update={"settings": settings.model_copy(update={
        "shorts_layout": "square", "shorts_square_rois": {
            sid: Roi(x=0.2, y=0.05, width=0.6, height=0.9),
        },
    })})
    square_plan = apply_square_layout(square, plan, root, None)
    preview = render(root, square, square_plan, tmp_path / "square-preview.mp4", preview=True)
    verify_output(preview, plan.actual_frame_count, plan.output_fps, expected_audio=True)
    # Changing the music must rebuild musical cuts from the selected version's snapshot.
    from sculptcut.remusic import replace_music
    from sculptcut.variants import read_variant

    folder = tmp_path / "new-music"
    folder.mkdir()
    pulse_track(folder / "pulse.wav", [0.55 + i * 0.65 for i in range(23)], seconds=16)
    changed = replace_music(root, output, folder, 7, None)
    state = read_variant(changed)
    changed_plan = RenderPlan.model_validate(state["plan"])
    assert changed_plan.settings["music_fingerprint"] != plan.settings["music_fingerprint"]
    assert changed_plan.decisions != plan.decisions
    assert all(d.action in ("DROP", "SPEED", "KEEP") for d in changed_plan.decisions)
    assert state["project"]["overrides"] == project.model_dump(mode="json")["overrides"]
    verify_output(changed, changed_plan.actual_frame_count, changed_plan.output_fps,
                  expected_audio=True)
