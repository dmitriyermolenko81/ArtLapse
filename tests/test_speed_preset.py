from pathlib import Path

from PySide6.QtWidgets import QApplication
from test_timeline import make_source

from sculptcut.domain import Project, ProjectSettings
from sculptcut.media import discover, run_process
from sculptcut.planning import plan_project, simple_speed_plan, validate_plan
from sculptcut.project import create_project
from sculptcut.rendering import render, verify_output
from sculptcut.ui import MainWindow


def test_simple_speed_preserves_all_sources_and_uniform_speed() -> None:
    project = Project(
        sources=[make_source(0, 0, 120_000_000), make_source(1, 120_000_000, 180_000_000)],
        settings=ProjectSettings(preset="speed"),
    )
    plan = simple_speed_plan(project, 60)
    validate_plan(project, plan)
    assert plan.actual_frame_count == plan.target_frame_count == 1800
    assert all(d.action == "SPEED" for d in plan.decisions)
    assert [(d.source_start_us, d.source_end_us) for d in plan.decisions] == [
        (0, 120_000_000),
        (0, 180_000_000),
    ]
    speeds = [
        (d.source_end_us - d.source_start_us) * plan.output_fps / (1_000_000 * d.output_frame_count)
        for d in plan.decisions
    ]
    assert speeds == [5, 5]


def test_speed_preset_skips_analysis_and_exports_real_video(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=160x120:r=15:d=4",
            "-c:v",
            "libx264",
            str(source),
        ]
    )
    root = tmp_path / "project"
    original = create_project(root, [source])
    project = original.model_copy(
        update={"settings": original.settings.model_copy(update={"preset": "speed"})}
    )
    plan = plan_project(root, project, 2)
    assert not list((root / "proxies").glob("*.mp4"))
    output = render(root, project, plan, tmp_path / "speed.mp4", encoder="libx264")
    verify_output(output, 60, 30)


def test_speed_preset_ui_runs_direct_export() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    actions: list[str] = []
    window.start_action = actions.append  # type: ignore[method-assign]
    try:
        window.target.setValue(2.5)
        window.preset.setCurrentIndex(window.preset.findData("speed"))
        assert window.target.isEnabled() and window.target.value() == 2.5
        window.create_or_export()
        assert actions == ["create"]
    finally:
        window.close()
        app.processEvents()
