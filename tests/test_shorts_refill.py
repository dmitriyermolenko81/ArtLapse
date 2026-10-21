from pathlib import Path
from threading import Event

import pytest
from test_timeline import make_source

from sculptcut.domain import Project, ProjectSettings
from sculptcut.media import discover, run_process
from sculptcut.planning import RenderDecision, build_plan, validate_plan
from sculptcut.project import create_project, save_project
from sculptcut.rendering import render, verify_output
from sculptcut.shorts_refill import rebuild_short_after_cuts, refill_plan_after_cuts
from sculptcut.variants import read_variant


def test_refill_keeps_cut_removed_and_restores_short_budget() -> None:
    project = Project(
        sources=[make_source(0, 0, 120_000_000)],
        settings=ProjectSettings(preset="shorts"),
    )
    original = build_plan(project, [], 60)

    rebuilt = refill_plan_after_cuts(project, original, [(10_000_000, 15_000_000)])

    validate_plan(project, rebuilt)
    assert rebuilt.actual_frame_count == rebuilt.target_frame_count == 1_800
    removed = [d for d in rebuilt.decisions if d.provenance == "user:shorts_manual_cut"]
    assert removed
    assert all(d.action == "DROP" and d.output_frame_count == 0 for d in removed)
    assert sum(d.output_frame_count for d in rebuilt.decisions) == 1_800


def test_refill_reports_when_remaining_footage_cannot_reach_target() -> None:
    project = Project(
        sources=[make_source(0, 0, 60_000_000)],
        settings=ProjectSettings(preset="shorts"),
    )
    plan = build_plan(project, [], 60)

    with pytest.raises(ValueError, match="недостаточно исходного материала"):
        refill_plan_after_cuts(project, plan, [(10_000_000, 15_000_000)])


def test_refill_does_not_accelerate_or_extend_process_final() -> None:
    project = Project(
        sources=[make_source(0, 0, 120_000_000)],
        settings=ProjectSettings(preset="shorts"),
    )
    base = build_plan(project, [], 60)
    plan = base.model_copy(
        update={
            "decisions": [
                RenderDecision(
                    source_id="0", source_start_us=0, source_end_us=116_000_000,
                    action="SPEED", output_frame_count=1_680,
                    provenance="user:process_story",
                ),
                RenderDecision(
                    source_id="0", source_start_us=116_000_000, source_end_us=120_000_000,
                    action="SPEED", output_frame_count=120,
                    provenance="user:shorts_final",
                ),
            ],
            "settings": base.settings | {"story": "process", "final_view_frames": 120},
        }
    )
    rebuilt = refill_plan_after_cuts(project, plan, [(10_000_000, 11_000_000)])
    assert rebuilt.actual_frame_count == 1_800
    final = rebuilt.decisions[-1]
    assert final.output_frame_count == 120
    assert rebuilt.settings["final_view_frames"] == 120
    shortened = refill_plan_after_cuts(project, plan, [(57_000_000, 58_000_000)])
    assert shortened.settings["final_view_frames"] == 90
    assert sum(
        d.output_frame_count for d in shortened.decisions if d.provenance == "user:shorts_final"
    ) == 90


def test_real_short_rebuild_preserves_full_duration_and_history(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=160x120:r=30:d=1",
            "-c:v",
            "libx264",
            str(source),
        ]
    )
    root = tmp_path / "project"
    project = create_project(root, [source]).model_copy(
        update={"settings": ProjectSettings(preset="shorts", output_name="Dragon")}
    )
    save_project(root, project)
    plan = build_plan(project, [], 0.6)
    original = root / "renders" / "Dragon.mp4"
    render(root, project, plan, original, Event(), encoder="libx264")

    result = rebuild_short_after_cuts(
        root,
        original,
        [(200_000, 400_000)],
        Event(),
        lambda _value: None,
        encoder="libx264",
    )

    verify_output(result, 18, 30)
    assert original.is_file()
    assert result.name == "Dragon-edit-001.mp4"
    state = read_variant(result)
    assert state["operation"] == "shorts_refill"
    assert state["parent"] == "Dragon.mp4"
    assert state["cuts_us"] == [[200_000, 400_000]]
    rebuilt = state["plan"]
    assert rebuilt["actual_frame_count"] == rebuilt["target_frame_count"] == 18
    assert any(d["provenance"] == "user:shorts_manual_cut" for d in rebuilt["decisions"])
