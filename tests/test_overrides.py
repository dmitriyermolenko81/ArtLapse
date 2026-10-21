from pathlib import Path

import pytest
from test_timeline import make_source

from sculptcut.domain import Override, Project
from sculptcut.media import discover, run_process
from sculptcut.overrides import apply_overrides
from sculptcut.planning import build_plan
from sculptcut.project import create_project
from sculptcut.rendering import render, verify_output


def test_override_preserves_budget_and_user_keep() -> None:
    project = Project(sources=[make_source(0, 0, 10_000_000)])
    plan = build_plan(project, [], 2)
    updated = project.model_copy(
        update={"overrides": [Override(source_id="0", start_us=0, end_us=1_000_000, action="KEEP")]}
    )
    result = apply_overrides(updated, plan)
    assert result.actual_frame_count == 60
    assert result.decisions[0].action == "KEEP"
    assert result.decisions[0].output_frame_count == 30
    assert project.overrides == []
    dropped = project.model_copy(
        update={
            "overrides": [Override(source_id="0", start_us=0, end_us=10_000_000, action="DROP")]
        }
    )
    with pytest.raises(ValueError, match="drop all"):
        apply_overrides(dropped, plan)


def test_invalid_overlapping_overrides() -> None:
    with pytest.raises(ValueError, match="overlap"):
        Project(
            sources=[make_source(0, 0, 10_000_000)],
            overrides=[
                Override(source_id="0", start_us=0, end_us=2_000_000, action="KEEP"),
                Override(source_id="0", start_us=1_000_000, end_us=3_000_000, action="DROP"),
            ],
        )


def test_legacy_retain_keeps_footage_accelerated_with_target_budget() -> None:
    project = Project(sources=[make_source(0, 0, 600_000_000)])
    plan = build_plan(project, [], 60)
    retained = project.model_copy(
        update={
            "overrides": [
                Override(
                    source_id="0",
                    start_us=start * 1_000_000,
                    end_us=(start + 100) * 1_000_000,
                    action="RETAIN",
                )
                for start in (0, 100, 200, 300)
            ]
        }
    )
    result = apply_overrides(retained, plan)
    assert result.actual_frame_count == 60 * result.output_fps
    assert all(
        decision.action == "SPEED" and decision.output_frame_count < 100 * result.output_fps
        for decision in result.decisions
        if decision.provenance == "user:RETAIN"
    )
    normal_speed = project.model_copy(
        update={
            "overrides": [Override(source_id="0", start_us=0, end_us=100_000_000, action="KEEP")]
        }
    )
    assert apply_overrides(normal_speed, plan).actual_frame_count > result.actual_frame_count


def test_legacy_retain_exports_shortened_video(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=160x120:r=15:d=12",
            "-c:v",
            "libx264",
            str(source),
        ]
    )
    root = tmp_path / "project"
    project = create_project(root, [source])
    project = project.model_copy(
        update={
            "overrides": [
                Override(
                    source_id=project.sources[0].id,
                    start_us=2_000_000,
                    end_us=10_000_000,
                    action="RETAIN",
                )
            ]
        }
    )
    plan = apply_overrides(project, build_plan(project, [], 2))
    assert plan.actual_frame_count == 2 * plan.output_fps
    assert any(d.provenance == "user:RETAIN" and d.action == "SPEED" for d in plan.decisions)
    output = render(root, project, plan, tmp_path / "short.mp4", encoder="libx264")
    verify_output(output, plan.actual_frame_count, plan.output_fps)
