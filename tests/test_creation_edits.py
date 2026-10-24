"""Main creation consumes pending output trims, in addition to episode corrections."""

from pathlib import Path
from threading import Event

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from test_combined_edits import PRESETS, episode_changes, fixture_project

from sculptcut.combined_edits import (
    creation_edit,
    prepare_combined_edit,
    prepare_refill_edit,
    render_combined_edit,
)
from sculptcut.domain import Project, ProjectSettings
from sculptcut.fixtures import generate_fixture
from sculptcut.intro import output_frame_count
from sculptcut.media import discover, run_process
from sculptcut.output_cuts import cut_output
from sculptcut.planning import RenderPlan, build_plan, validate_plan
from sculptcut.project import create_project, load_project, save_project
from sculptcut.rendering import render, verify_output
from sculptcut.shorts_layout import MUSIC_SHORTS_PRESETS
from sculptcut.ui import MainWindow
from sculptcut.variants import read_variant


@pytest.mark.parametrize("preset", PRESETS)
def test_creation_transfers_pending_trims_to_new_settings_and_repeated_exports(
    tmp_path: Path, preset: str,
) -> None:
    root, project, _, video = fixture_project(tmp_path)
    before = {p: p.read_bytes() for p in (video, project.sources[0].path)}
    previous = prepare_combined_edit(root, video, [(0, 1_000_000)], apply_current=False)
    settings = ProjectSettings.model_validate(project.settings.model_dump() | {
        "preset": preset, "target_seconds": 6,
    })
    project = project.model_copy(update={"settings": settings})
    fresh = build_plan(project, [], 6)
    fresh = fresh.model_copy(update={"settings": fresh.settings | {"width": 160, "height": 120}})
    combined = creation_edit(project, fresh, previous)
    assert combined.plan.actual_frame_count == 120
    assert min(d.source_start_us for d in combined.plan.decisions
               if d.output_frame_count) == 3_000_000
    validate_plan(project, combined.plan)
    output = root / "renders" / "created.mp4"
    render_combined_edit(root, combined, output, Event(), lambda _: None, encoder="libx264")
    verify_output(output, 120, 30)
    reopened = prepare_combined_edit(root, output, apply_current=False)
    repeated = creation_edit(project, fresh, reopened)
    assert repeated.plan.decisions == combined.plan.decisions
    assert all(p.read_bytes() == value for p, value in before.items())


@pytest.mark.parametrize("saved", [False, True])
def test_creation_applies_both_cut_layers_and_current_episodes(tmp_path: Path, saved: bool) -> None:
    root, project, plan, video = fixture_project(tmp_path)
    if saved:
        video = cut_output(video, [(0, 500_000)], Event(), lambda _: None)
        pending = [(0, 500_000)]
    else:
        pending = [(0, 1_000_000)]
    project = episode_changes(root, project)
    previous = prepare_combined_edit(root, video, pending, apply_current=False)
    combined = creation_edit(project, plan, previous)
    assert all(d.source_start_us >= 4_500_000 for d in combined.plan.decisions
               if d.output_frame_count)
    assert sum(d.output_frame_count for d in combined.plan.decisions
               if d.provenance == "user:SET_SPEED") == 45
    output = root / "renders" / "created.mp4"
    render_combined_edit(root, combined, output, Event(), lambda _: None, encoder="libx264")
    verify_output(output, 60, 30)


def test_creation_preserves_partial_holds_and_bookend_masks(tmp_path: Path) -> None:
    root, project, plan, video = fixture_project(tmp_path, cover=True)
    decision = plan.decisions[0].model_copy(update={
        "action": "HOLD", "provenance": "user:test",
    })
    plan = plan.model_copy(update={"decisions": [decision]})
    video = root / "renders" / "stills.mp4"
    render(root, project, plan, video, Event(), encoder="libx264")
    offset = (output_frame_count(project.settings, 30, plan.actual_frame_count)
              - plan.actual_frame_count)
    pending = [(0, 500_000), (offset * 1_000_000 // 30, (offset + 10) * 1_000_000 // 30)]
    previous = prepare_combined_edit(root, video, pending, apply_current=False)
    combined = creation_edit(project, plan, previous)
    assert combined.leading == [(0, 15)]
    assert combined.plan.actual_frame_count == plan.actual_frame_count - 10
    output = root / "renders" / "created.mp4"
    render_combined_edit(root, combined, output, Event(), lambda _: None, encoder="libx264")
    verify_output(output, output_frame_count(project.settings, 30,
                                            combined.plan.actual_frame_count) - 15, 30)
    again = creation_edit(project, plan, prepare_combined_edit(root, output, apply_current=False))
    assert again.plan.actual_frame_count == combined.plan.actual_frame_count
    assert again.leading == combined.leading


def wait_creation(window: MainWindow) -> None:
    timer = QTimer()
    timer.setSingleShot(True)
    timer.start(90_000)
    while window.job is not None and timer.isActive():
        loop = QEventLoop()
        window.job.finished.connect(loop.quit)
        timer.timeout.connect(loop.quit)
        loop.exec()
    timer.stop()
    assert window.job is None and window.job_succeeded, window.last_error


def creation_fixture(tmp_path: Path) -> tuple[Path, Project, RenderPlan, Path]:
    source = generate_fixture(tmp_path / "source.mp4", "local", duration=9, fps=30)
    root = tmp_path / "project"
    project = create_project(root, [source]).model_copy(update={
        "settings": ProjectSettings(target_seconds=3),
    })
    save_project(root, project)
    plan = build_plan(project, [], 3)
    plan = plan.model_copy(update={"settings": plan.settings | {"width": 192, "height": 128}})
    video = root / "renders" / "original.mp4"
    render(root, project, plan, video, Event(), encoder="libx264")
    return root, project, plan, video


def test_creation_keeps_source_mask_through_a_hold_then_moving_mode(tmp_path: Path) -> None:
    root, project, plan, video = fixture_project(tmp_path)
    previous = prepare_combined_edit(root, video, [(1_000_000, 1_500_000)], apply_current=False)
    still = plan.model_copy(update={"decisions": [plan.decisions[0].model_copy(update={
        "action": "HOLD", "provenance": "user:test",
    })]})
    combined = creation_edit(project, still, previous)
    assert combined.plan.actual_frame_count == 90
    assert [(d.source_start_us, d.source_end_us) for d in combined.plan.decisions
            if d.provenance == "user:shorts_manual_cut"] == [(3_000_000, 4_500_000)]
    output = root / "renders" / "hold.mp4"
    render_combined_edit(root, combined, output, Event(), lambda _: None, encoder="libx264")
    verify_output(output, 90, 30)
    moving = creation_edit(project, plan, prepare_combined_edit(root, output, apply_current=False))
    assert moving.plan.actual_frame_count == 75
    assert not any(d.output_frame_count and d.source_start_us < 4_500_000
                   and d.source_end_us > 3_000_000 for d in moving.plan.decisions)


def test_creation_retains_saved_refill_policy_at_new_duration(tmp_path: Path) -> None:
    root, project, _, video = fixture_project(tmp_path)
    previous = prepare_refill_edit(root, video, [(0, 1_000_000)])
    fresh = build_plan(project, [], 6)
    fresh = fresh.model_copy(update={"settings": fresh.settings | {"width": 160, "height": 120}})
    combined = creation_edit(project, fresh, previous)
    assert combined.plan.actual_frame_count == 180
    assert min(d.source_start_us for d in combined.plan.decisions
               if d.output_frame_count) == 3_000_000
    output = root / "renders" / "refilled.mp4"
    render_combined_edit(root, combined, output, Event(), lambda _: None, encoder="libx264")
    verify_output(output, 180, 30)


def test_creation_source_boundaries_do_not_slow_one_x_fragments(tmp_path: Path) -> None:
    root, project, _, _ = fixture_project(tmp_path)
    old = build_plan(project, [], 7)
    old = old.model_copy(update={"settings": old.settings | {"width": 160, "height": 120}})
    video = root / "renders" / "seven-seconds.mp4"
    render(root, project, old, video, Event(), encoder="libx264")
    previous = prepare_combined_edit(root, video, [(33_334, 66_666)], apply_current=False)
    fresh = build_plan(project, [], 9)
    combined = creation_edit(project, fresh, previous)
    validate_plan(project, combined.plan)
    assert [(d.source_start_us, d.source_end_us) for d in combined.plan.decisions
            if d.provenance == "user:shorts_manual_cut"] == [(42_857, 85_714)]
    assert combined.plan.actual_frame_count == 268


@pytest.mark.parametrize("preset", PRESETS)
@pytest.mark.parametrize("with_episodes", [False, True])
def test_real_create_button_uses_unsaved_cuts_and_repeats_them(
    tmp_path: Path, preset: str, with_episodes: bool,
) -> None:
    root, project, _, video = creation_fixture(tmp_path)
    if with_episodes:
        episode_changes(root, project)
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.show()
        window.open_project_folder(root)
        window.preset.setCurrentIndex(window.preset.findData(preset))
        window.target.setValue(3 if window.duration_in_seconds else 0.05)
        if preset in MUSIC_SHORTS_PRESETS:
            music = tmp_path / "music.wav"
            run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                         "sine=frequency=440:duration=9", str(music)])
            window.music.setText(str(music))
        window.output_name.setText("Created")
        window.open_video(video, "Saved")
        for _ in range(150):
            if window.player.duration() == 3000:
                break
            QTest.qWait(20)
        window.toggle_output_editor()
        window.output_editor.start.setValue(0)
        window.output_editor.end.setValue(1)
        window.output_editor.add_cut()
        assert window.output_editor.cuts == [(0, 1_000_000)]
        for _ in range(2):
            window.create_button.click()
            assert window.job is not None
            wait_creation(window)
            result = window.last_result_video
            state = read_variant(result)
            plan = RenderPlan.model_validate(state["plan"])
            assert state["operation"] == "combined_edit"
            assert all(d.source_start_us >= (4_500_000 if with_episodes else 3_000_000)
                       for d in plan.decisions
                       if d.output_frame_count)
            assert plan.settings["preset"] == preset
            verify_output(result, plan.actual_frame_count, plan.output_fps,
                          expected_audio=preset in MUSIC_SHORTS_PRESETS)
            assert not window.output_editor.cuts
        assert load_project(root).sources == project.sources
    finally:
        if window.job is not None:
            window.job.cancel.set()
            window.job.wait(30_000)
        window.close()
        app.processEvents()
