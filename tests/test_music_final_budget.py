"""Dense musical grids must reserve the full final presentation's frame budget."""

from pathlib import Path

import pytest
from test_timeline import make_source

from sculptcut.domain import Project, ProjectSettings
from sculptcut.planning import build_plan, validate_plan
from sculptcut.process_story import process_plan
from sculptcut.segmentation import Segment


@pytest.mark.parametrize("fps", [24, 30, 60])
@pytest.mark.parametrize("final_seconds", [.4, 1.1, 7])
def test_dense_grid_reserves_entire_final_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fps: int, final_seconds: float,
) -> None:
    source = make_source(0, 0, 600_000_000)
    project = Project(sources=[source], settings=ProjectSettings(
        preset="music_shorts", output_fps=fps, target_seconds=30,
    ))
    end = 30 * fps
    # A long final music slot can require more than the normal four-second finale.
    last_slot = round(final_seconds * fps)
    final_frames = max(4 * fps, last_slot)
    step = round(.4 * fps)
    grid = [*range(0, end - last_slot, step), end - last_slot, end]
    grid = sorted(set(grid))
    monkeypatch.setattr("sculptcut.music_episodes.music_episode_grid", lambda *args: grid)
    monkeypatch.setattr("sculptcut.process_story.project_screens", lambda *args: [])
    monkeypatch.setattr("sculptcut.sculpt_only.final_start",
                        lambda root, project, source, length, *args: source.duration_us - length)
    monkeypatch.setattr("sculptcut.shorts_quality.EpisodeInspector.score", lambda *args: 1.0)
    monkeypatch.setattr("sculptcut.shorts_quality.EpisodeInspector.signature", lambda *args: None)
    segments = [Segment(source_id="0", start_us=0, end_us=source.duration_us,
                        kind="SCULPT", score=1)]
    plan = process_plan(tmp_path, project, build_plan(project, [], 30), segments)
    validate_plan(project, plan)
    assert plan.actual_frame_count == 30 * fps
    finale = [d for d in plan.decisions if d.provenance == "user:shorts_final"]
    assert sum(d.output_frame_count for d in finale) == final_frames
    work = [d for d in plan.decisions if d.provenance == "user:process_story"]
    assert work
    assert sum(d.output_frame_count for d in work) == end - final_frames
    assert all((d.source_end_us - d.source_start_us) * fps
               <= d.output_frame_count * 8_000_000 for d in work)


def test_automatic_finale_leaves_work_budget_in_short_clip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_source(0, 0, 60_000_000)
    project = Project(sources=[source], settings=ProjectSettings(preset="music_shorts"))
    monkeypatch.setattr("sculptcut.music_episodes.music_episode_grid",
                        lambda *args: [0, 15, 30, 45, 60])
    monkeypatch.setattr("sculptcut.process_story.project_screens", lambda *args: [])
    monkeypatch.setattr("sculptcut.sculpt_only.final_start",
                        lambda root, project, source, length, *args: source.duration_us - length)
    monkeypatch.setattr("sculptcut.shorts_quality.EpisodeInspector.score", lambda *args: 1.0)
    monkeypatch.setattr("sculptcut.shorts_quality.EpisodeInspector.signature", lambda *args: None)
    segments = [Segment(source_id="0", start_us=0, end_us=source.duration_us,
                        kind="SCULPT", score=1)]
    plan = process_plan(tmp_path, project, build_plan(project, [], 2), segments)
    validate_plan(project, plan)
    assert plan.actual_frame_count == 60
    assert sum(d.output_frame_count for d in plan.decisions
               if d.provenance == "user:shorts_final") == 59
    assert sum(d.output_frame_count for d in plan.decisions
               if d.provenance == "user:process_story") == 1
