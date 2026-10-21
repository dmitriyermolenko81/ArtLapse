from pathlib import Path

from test_timeline import make_source

from sculptcut.domain import Override, Project
from sculptcut.idle import IdleRange
from sculptcut.overrides import apply_overrides
from sculptcut.pacing import paced_plan
from sculptcut.planning import build_plan, validate_plan
from sculptcut.segmentation import Segment


def test_pacing_contrast_budget_and_final_tail() -> None:
    project = Project(sources=[make_source(0, 0, 600_000_000)])
    segments = [
        Segment(
            source_id="0",
            start_us=0,
            end_us=600_000_000,
            kind="SCULPT",
            score=0.9,
            components={"observed_change": 0.02, "novelty": 0.01},
        )
    ]
    base = build_plan(
        project, [IdleRange(source_id="0", start_us=590_000_000, end_us=600_000_000)], 60
    )
    plan = paced_plan(project, base, segments)
    validate_plan(project, plan)
    assert plan.actual_frame_count == 1800
    tail = plan.decisions[-1]
    assert tail.action == "KEEP" and tail.output_frame_count == 300
    assert tail.source_start_us == 590_000_000
    anchor, middle = plan.decisions[:2]

    def speed(index: int) -> float:
        d = plan.decisions[index]
        return (d.source_end_us - d.source_start_us) * 30 / 1e6 / d.output_frame_count

    assert anchor.provenance == "pacing:anchor"
    assert middle.provenance == "pacing:repetitive_middle"
    assert speed(1) > speed(0) * 4
    assert all(d.action != "DROP" for d in plan.decisions)
    assert plan == paced_plan(project, base, segments)
    manual = project.model_copy(
        update={"overrides": [Override(source_id="0", start_us=0, end_us=1_000_000, action="DROP")]}
    )
    revised = apply_overrides(manual, plan)
    assert revised.decisions[0].action == "DROP"
    assert revised.decisions[-1].output_frame_count == 300


def test_uncertain_middle_retained_and_fractional_final_time() -> None:
    project = Project(sources=[make_source(0, 0, 60_033_333)])
    segment = Segment(source_id="0", start_us=0, end_us=60_033_333, kind="UNKNOWN", score=0)
    plan = paced_plan(project, build_plan(project, [], 7.1), [segment])
    validate_plan(project, plan)
    assert plan.actual_frame_count == 213
    assert all(d.action != "DROP" for d in plan.decisions)
    assert all("repetitive_middle" not in d.provenance for d in plan.decisions)
    assert plan.decisions[-1].output_frame_count == 42
    missing = paced_plan(project, build_plan(project, [], 7.1), [])
    assert all("retain_uncertain" in d.provenance for d in missing.decisions[:-1])


def test_navigation_receives_less_time_than_equal_brush_work() -> None:
    project = Project(sources=[make_source(0, 0, 250_000_000)])
    segments = [
        Segment(
            source_id="0",
            start_us=i * 120_000_000,
            end_us=(i + 1) * 120_000_000,
            kind=kind,
            score=0.9,
            components={"observed_change": 0.5, "novelty": 0.5},
        )
        for i, kind in enumerate(("SCULPT", "CAMERA"))
    ]
    plan = paced_plan(project, build_plan(project, [], 60), segments)
    work = sum(d.output_frame_count for d in plan.decisions if d.source_end_us <= 120_000_000)
    navigation = sum(
        d.output_frame_count
        for d in plan.decisions
        if 120_000_000 <= d.source_start_us and d.source_end_us <= 240_000_000
    )
    assert work > navigation * 2
    assert plan.actual_frame_count == 1800


def test_paced_real_export_and_preview(tmp_path: Path) -> None:
    from sculptcut.media import discover, run_process
    from sculptcut.project import create_project
    from sculptcut.proxy import make_proxies
    from sculptcut.rendering import render, verify_output

    source = tmp_path / "sculpt.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=128x96:r=30:d=31",
            "-c:v",
            "libx264",
            str(source),
        ]
    )
    root = tmp_path / "project"
    project = create_project(root, [source])
    segments = [
        Segment(
            source_id="source-0000",
            start_us=0,
            end_us=31_000_000,
            kind="SCULPT",
            score=0.9,
            components={"observed_change": 0.05},
        )
    ]
    plan = paced_plan(project, build_plan(project, [], 5), segments)
    make_proxies(root, project)
    for preview in (False, True):
        output = render(
            root,
            project,
            plan,
            tmp_path / f"result-{preview}.mp4",
            preview=preview,
            encoder="libx264",
        )
        verify_output(output, 150, 30)
