from pathlib import Path
from threading import Event

import cv2
import pytest
from test_timeline import make_source

from sculptcut.domain import CameraPoint, Project, ProjectSettings, Roi, SubjectCamera
from sculptcut.media import Cancelled, MediaError, discover, run_process
from sculptcut.planning import RenderDecision, build_plan
from sculptcut.project import create_project, save_project
from sculptcut.render_batches import coalesce_chunks
from sculptcut.rendering import chunk_decisions, render, verify_output


def pair() -> tuple[Project, list[RenderDecision]]:
    project = Project(sources=[make_source(0, 0, 4_000_000)],
                      settings=ProjectSettings(preset="speed"))
    decision = build_plan(project, [], 2).decisions[0]
    return project, [decision.model_copy(update={
        "source_start_us": index * 2_000_000, "source_end_us": (index + 1) * 2_000_000,
        "output_frame_count": 30,
    }) for index in range(2)]


def test_join_preserves_plan_and_respects_execution_limits() -> None:
    project, pieces = pair()
    before = [p.model_dump() for p in pieces]
    joined = coalesce_chunks(pieces, {s.id: s for s in project.sources}, 30)
    assert len(joined) == 1
    assert (joined[0].source_start_us, joined[0].source_end_us,
            joined[0].output_frame_count) == (0, 4_000_000, 60)
    assert [p.model_dump() for p in pieces] == before
    long = Project(sources=[make_source(0, 0, 18_000_000_000)])
    plan = build_plan(long, [], 600)
    bounded = coalesce_chunks(chunk_decisions(plan), {s.id: s for s in long.sources}, 30)
    assert sum(p.output_frame_count for p in bounded) == 18000
    assert all(p.source_end_us - p.source_start_us <= 300_000_000 for p in bounded)
    assert all(p.output_frame_count <= 900 for p in bounded)


@pytest.mark.parametrize("change", [
    {"source_start_us": 2_100_000}, {"output_frame_count": 29}, {"action": "HOLD"},
    {"action": "KEEP"}, {"crop": Roi(x=0, y=0, width=.5, height=1)},
    {"provenance": "user:final_tail"}, {"sample_times_us": [2_000_000]},
    {"provenance": "different boundary"},
    {"subject_camera": SubjectCamera(viewport=Roi(x=0, y=0, width=1, height=1),
                                    width=1, height=1,
                                    points=[CameraPoint(time_us=2_000_000, x=.5, y=.5)],
                                    background=0)},
])
def test_incompatible_boundaries_are_kept(change: dict[str, object]) -> None:
    project, pieces = pair()
    pieces[1] = pieces[1].model_copy(update=change)
    assert coalesce_chunks(pieces, {s.id: s for s in project.sources}, 30) == pieces


def test_source_change_is_an_execution_boundary() -> None:
    project, pieces = pair()
    other = project.sources[0].model_copy(update={"id": "other"})
    pieces[1] = pieces[1].model_copy(update={"source_id": "other"})
    sources = {project.sources[0].id: project.sources[0], "other": other}
    assert coalesce_chunks(pieces, sources, 30) == pieces


@pytest.mark.parametrize("origin,denominator", [(3_000_000, 1), (0, 1001)])
def test_offset_and_fractional_off_grid_sources_keep_their_clocks(
    origin: int, denominator: int,
) -> None:
    project, pieces = pair()
    source = project.sources[0].model_copy(update={
        "start_time": origin, "fps_num": 30 if denominator == 1 else 30000,
        "fps_den": denominator,
    })
    assert coalesce_chunks(pieces, {source.id: source}, 30) == pieces


@pytest.fixture
def alternating(tmp_path: Path) -> Path:
    source = tmp_path / "source.mp4"
    run_process([
        discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
        "color=black:s=160x90:r=30:d=4", "-vf",
        "drawbox=c=red:t=fill:enable='lt(mod(n,12),6)',"
        "drawbox=c=blue:t=fill:enable='gte(mod(n,12),6)'",
        "-c:v", "libx264", str(source),
    ])
    return source


def test_actual_export_keeps_every_boundary_and_resumes_merged_work(
    alternating: Path, tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project = create_project(project_root, [alternating]).model_copy(update={
        "settings": ProjectSettings(preset="speed"),
    })
    save_project(project_root, project)
    plan = build_plan(project, [], 2)
    first = plan.decisions[0]
    plan = plan.model_copy(update={"decisions": [first.model_copy(update={
        "source_start_us": i * 200_000, "source_end_us": (i + 1) * 200_000,
        "output_frame_count": 3,
    }) for i in range(20)]})
    original = plan.model_dump_json()
    event = Event()
    output = tmp_path / "result.mp4"
    with pytest.raises(Cancelled):
        render(project_root, project, plan, output, event, encoder="libx264",
               progress=lambda p: event.set() if p >= .85 else None)
    assert not output.exists()
    receipts = list(tmp_path.rglob("chunk-*.mp4.json"))
    assert len(receipts) == 1  # Completed merged work is safely checkpointed.
    event.clear()
    render(project_root, project, plan, output, event, encoder="libx264")
    verify_output(output, 60, 30)
    assert plan.model_dump_json() == original
    capture = cv2.VideoCapture(str(output))
    try:
        for index in range(60):
            ok, frame = capture.read()
            assert ok
            means = frame.mean(axis=(0, 1))
            assert means[2 if (index // 3) % 2 == 0 else 0] > 200
    finally:
        capture.release()
    assert not list(tmp_path.rglob("chunk-*.mp4.json"))


def test_combined_probe_still_rejects_wrong_frame_budget(alternating: Path) -> None:
    verify_output(alternating, 120, 30)
    with pytest.raises(MediaError, match="frame count"):
        verify_output(alternating, 240, 60)  # Same duration, wrong frame count/clock.
    with pytest.raises(MediaError, match="duration differs"):
        verify_output(alternating, 119, 30)
    with pytest.raises(MediaError, match="audio stream"):
        verify_output(alternating, 120, 30, expected_audio=True)
