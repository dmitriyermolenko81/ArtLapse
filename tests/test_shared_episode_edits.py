"""Source-time edits survive new exports and every automatic preset."""

import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication
from test_beat_shorts import pulse_track
from test_timeline import make_source

from sculptcut.analysis import extract_features
from sculptcut.domain import Override, Project, ProjectSettings
from sculptcut.episode_editor import edit_episode
from sculptcut.episode_timeline import trim_episode
from sculptcut.fixtures import generate_fixture
from sculptcut.media import discover, run_process
from sculptcut.overrides import apply_overrides, exclude_dropped
from sculptcut.planning import RenderPlan, build_plan, plan_project, validate_plan
from sculptcut.project import create_project, load_project, save_project
from sculptcut.rendering import render, verify_output
from sculptcut.ui import MainWindow


def test_shared_edits_cross_all_presets_with_simple_speed_exception(tmp_path: Path) -> None:
    video = tmp_path / "drawing.mp4"
    run_process([
        discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
        "color=c=0x222222:s=320x180:r=10:d=12,"
        "drawbox=x=48:y=12:w=224:h=156:color=0xe0c080:t=fill,"
        "drawbox=x=125:y=35:w=70:h=105:color=0x553366:t=fill,"
        "drawbox=x=140:y=45:w=10:h=10:color=red:t=fill,"
        "drawbox=x=170:y=100:w=12:h=10:color=white:t=fill:enable='gte(t,3)',"
        "drawbox=x=165:y=120:w=22:h=10:color=white:t=fill:enable='gte(t,5)',"
        "drawbox=x=160:y=70:w=25:h=10:color=white:t=fill:enable='gte(t,7)'",
        "-c:v", "libx264", "-threads", "1", str(video),
    ])
    track = tmp_path / "music.wav"
    pulse_track(track, [0.3, 0.8, 1.3, 1.8, 2.3, 2.8, 3.3, 3.8])
    root = tmp_path / "project"
    project = create_project(root, [video])
    decision = build_plan(project, [], 4).decisions[0]
    project = edit_episode(project, decision.model_copy(update={
        "source_start_us": 2_000_000, "source_end_us": 4_000_000,
    }), None)
    project = trim_episode(project, decision.model_copy(update={
        "source_start_us": 6_000_000, "source_end_us": 9_000_000,
    }), 6_000_000, 8_000_000, 30)
    project = edit_episode(project, decision.model_copy(update={
        "source_start_us": 10_000_000, "source_end_us": 12_000_000,
    }), None)
    rules = project.overrides
    save_project(root, project)
    for preset in ("timelapse", "shorts", "music_shorts", "beat_shorts", "photoshop_shorts",
                   "photoshop_beat_shorts", "photoshop_music_shorts", "speed"):
        project = load_project(root)
        settings = ProjectSettings.model_validate(project.settings.model_dump() | {
            "preset": preset, "proxy_width": 320, "proxy_fps": 5,
            "music_path": track if "beat" in preset or "music" in preset else None,
            "final_view_seconds": 0, "music_fade_seconds": 0,
        })
        project = project.model_copy(update={"settings": settings})
        save_project(root, project)
        if preset in ("timelapse", "shorts", "music_shorts", "beat_shorts"):
            extract_features(root, project)
        plan = plan_project(root, project, 4)
        validate_plan(project, plan)
        assert load_project(root).overrides == rules
        if preset == "speed":
            assert all(d.action == "SPEED" for d in plan.decisions)
            assert sum(d.source_end_us - d.source_start_us for d in plan.decisions) == 12_000_000
            assert apply_overrides(project, plan) == plan
        else:
            for d in plan.decisions:
                if d.action != "DROP":
                    assert not any(
                        o.action == "DROP" and o.source_id == d.source_id
                        and o.start_us < d.source_end_us and o.end_us > d.source_start_us
                        for o in rules
                    ), (preset, d)
            if preset in ("timelapse", "shorts", "music_shorts", "photoshop_shorts"):
                assert any(d.provenance == "user:SET_SPEED" for d in plan.decisions)
        assert plan_project(root, project, 4) == plan
        if preset in ("timelapse", "photoshop_beat_shorts"):
            output = render(root, project, plan, tmp_path / f"{preset}.mp4", encoder="libx264")
            verify_output(
                output, plan.actual_frame_count, plan.output_fps, expected_audio="beat" in preset,
            )


def test_all_excluded_fails_instead_of_resurrecting_footage() -> None:
    project = Project(sources=[make_source(0, 0, 10_000_000)], overrides=[
        Override(source_id="0", start_us=0, end_us=10_000_000, action="DROP"),
    ])
    base = build_plan(project, [], 2)
    with pytest.raises(ValueError, match="drop all"):
        exclude_dropped(project, base)


def test_gui_export_remembers_episode_rules_after_speed_mode(tmp_path: Path) -> None:
    video = generate_fixture(tmp_path / "local.mp4", "local", duration=4)
    root = tmp_path / "project"
    project = create_project(root, [video])
    project = edit_episode(project, build_plan(project, [], 2).decisions[0].model_copy(update={
        "source_start_us": 1_000_000, "source_end_us": 2_000_000,
    }), None)
    save_project(root, project)
    rules = project.overrides
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.show()
        window.folder.setText(str(root))
        window.set_sources([video])
        window.output_name.setText("Shared edits")
        for preset in ("speed", "timelapse"):
            window.preset.setCurrentIndex(window.preset.findData(preset))
            window.target.setValue(2 / 60)
            window._start_checked_action("create")
            deadline = time.monotonic() + 30
            while window.job is not None and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            assert window.job is None, "Export did not finish"
            assert not window.last_error, window.last_error
            assert load_project(root).overrides == rules
            plan = RenderPlan.model_validate_json((root / "render_plan.json").read_text("utf-8"))
            assert any(d.action == "DROP" for d in plan.decisions) == (preset != "speed")
    finally:
        if window.job is not None:
            window.job.cancel.set()
            window.job.wait(30_000)
        window.close()
        app.processEvents()
