"""Trim and episode edits coexist on source time, in either order and on reopening."""

from pathlib import Path
from threading import Event
from typing import get_args

import pytest
from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from test_intro import make_image

from sculptcut.combined_edits import prepare_combined_edit, render_combined_edit
from sculptcut.cut_save_dialog import CutSaveDialog
from sculptcut.domain import Override, Project, ProjectSettings
from sculptcut.episode_panel import EpisodePanel, episode_export
from sculptcut.intro import body_frame_budget, output_frame_count
from sculptcut.media import discover, run_process
from sculptcut.output_cuts import cut_output
from sculptcut.overrides import apply_overrides
from sculptcut.planning import RenderPlan, build_plan, validate_plan
from sculptcut.project import create_project, load_project, save_project
from sculptcut.remusic import replace_music
from sculptcut.rendering import render, verify_output
from sculptcut.shorts_refill import rebuild_after_cuts
from sculptcut.ui import MainWindow
from sculptcut.variants import has_manual_edits, read_variant, save_variant

PRESETS = get_args(ProjectSettings.model_fields["preset"].annotation)


def fixture_project(tmp_path: Path, preset: str = "timelapse", cover: bool = False,
                    ) -> tuple[Path, Project, RenderPlan, Path]:
    source = tmp_path / "source.mp4"
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 "testsrc2=s=160x120:r=30:d=9", "-c:v", "libx264", str(source)])
    image = tmp_path / "intro.png"
    if cover:
        make_image(image, (0, 220, 0))
    settings = ProjectSettings.model_validate({
        "preset": preset, "intro_image": image if cover else None, "target_seconds": 3,
    })
    root = tmp_path / "project"
    project = create_project(root, [source]).model_copy(update={"settings": settings})
    save_project(root, project)
    plan = build_plan(project, [], body_frame_budget(settings, 30, 3) / 30)
    plan = plan.model_copy(update={"settings": plan.settings | {"width": 160, "height": 120}})
    (root / "render_plan.json").write_text(plan.model_dump_json(), encoding="utf-8")
    original = root / "renders" / "original.mp4"
    render(root, project, plan, original, Event(), encoder="libx264")
    return root, project, plan, original


def episode_changes(root: Path, project: Project) -> Project:
    changed = project.model_copy(update={"overrides": [
        Override(source_id=project.sources[0].id, start_us=3_000_000,
                 end_us=4_500_000, action="DROP"),
        Override(source_id=project.sources[0].id, start_us=6_000_000,
                 end_us=9_000_000, action="SET_SPEED", speed=2),
    ]})
    save_project(root, changed)
    return changed


def assert_combined(project: Project, plan: RenderPlan) -> None:
    validate_plan(project, plan)
    kept = [d for d in plan.decisions if d.output_frame_count]
    assert kept and all(d.source_start_us >= 4_500_000 for d in kept)
    assert sum(d.output_frame_count for d in kept if d.provenance == "user:SET_SPEED") == 45


@pytest.mark.parametrize("preset", PRESETS)
@pytest.mark.parametrize("policy", ["trim", "refill"])
def test_real_trim_then_episodes_never_restore_excluded_source(
    tmp_path: Path, preset: str, policy: str,
) -> None:
    root, project, _, original = fixture_project(tmp_path, preset)
    before = {p: p.read_bytes() for p in (original, project.sources[0].path,
                                         root / "render_plan.json")}
    trimmed = (cut_output(original, [(0, 1_000_000)], Event(), lambda _: None)
               if policy == "trim" else rebuild_after_cuts(
                   root, original, [(0, 1_000_000)], Event(), lambda _: None, encoder="libx264"))
    changed = episode_changes(root, project)
    edit = prepare_combined_edit(root, trimmed)
    assert_combined(edit.project, edit.plan)
    assert edit.plan.actual_frame_count == (60 if policy == "trim" else 90)
    result = root / "renders" / "combined.mp4"
    render_combined_edit(root, edit, result, Event(), lambda _: None, encoder="libx264")
    verify_output(result, edit.plan.actual_frame_count, 30)
    reopened = prepare_combined_edit(root, result)
    assert_combined(reopened.project, reopened.plan)
    assert has_manual_edits(result)
    assert load_project(root) == changed
    assert all(path.read_bytes() == data for path, data in before.items())


def test_pending_episodes_then_trim_and_refill_use_the_same_source_exclusions(
    tmp_path: Path,
) -> None:
    root, project, _, original = fixture_project(tmp_path)
    episode_changes(root, project)
    edit = prepare_combined_edit(root, original, [(0, 1_000_000)])
    assert_combined(edit.project, edit.plan)
    result = root / "renders" / "combined.mp4"
    render_combined_edit(root, edit, result, Event(), lambda _: None, encoder="libx264")
    verify_output(result, 60, 30)
    refilled = rebuild_after_cuts(root, original, [(0, 1_000_000)], Event(), lambda _: None,
                                 encoder="libx264")
    saved = read_variant(refilled)
    assert_combined(Project.model_validate(saved["project"]),
                    RenderPlan.model_validate(saved["plan"]))
    verify_output(refilled, 90, 30)
    # A later cut in the surviving source must preserve both older exclusion layers.
    again = prepare_combined_edit(root, result, [(0, 200_000)])
    assert_combined(again.project, again.plan)
    assert min(d.source_start_us for d in again.plan.decisions if d.output_frame_count) > 4_500_000


def test_trim_mask_survives_overlapping_episode_keep_and_speed(tmp_path: Path) -> None:
    root, project, plan, original = fixture_project(tmp_path)
    project = project.model_copy(update={"overrides": [Override(
        source_id=project.sources[0].id, start_us=0, end_us=9_000_000,
        action="SET_SPEED", speed=3,
    )]})
    save_project(root, project)
    edit = prepare_combined_edit(root, original, [(500_000, 1_000_000)])
    cuts = [d for d in edit.plan.decisions if d.provenance == "user:shorts_manual_cut"]
    assert [(d.source_start_us, d.source_end_us, d.output_frame_count) for d in cuts] == [
        (1_500_000, 3_000_000, 0),
    ]
    assert plan.decisions[0].output_frame_count == 90
    assert apply_overrides(project, edit.plan, manual=True).decisions == edit.plan.decisions


def test_episode_undo_after_export_keeps_trims_but_restores_episode_content(tmp_path: Path) -> None:
    root, project, _, original = fixture_project(tmp_path)
    changed = episode_changes(root, project)
    edit = prepare_combined_edit(root, original, [(0, 1_000_000)])
    result = root / "renders" / "combined.mp4"
    render_combined_edit(root, edit, result, Event(), lambda _: None, encoder="libx264")
    save_project(root, project)  # The global Undo restores the prior episode rule list.
    undone = prepare_combined_edit(root, result)
    kept = [d for d in undone.plan.decisions if d.output_frame_count]
    assert min(d.source_start_us for d in kept) == 3_000_000
    assert sum(d.source_end_us - d.source_start_us for d in kept) == 6_000_000
    assert all(d.provenance != "user:SET_SPEED" for d in kept)
    save_project(root, changed)
    redone = prepare_combined_edit(root, result)
    assert redone.plan.decisions == edit.plan.decisions


def test_successive_music_changes_preserve_episode_only_corrections(tmp_path: Path) -> None:
    root, project, _, original = fixture_project(tmp_path, "music_shorts")
    episode_changes(root, project)
    edit = prepare_combined_edit(root, original)
    result = root / "renders" / "combined.mp4"
    render_combined_edit(root, edit, result, Event(), lambda _: None, encoder="libx264")
    for index in range(2):
        music = tmp_path / f"music-{index}.wav"
        run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                     f"sine=frequency={440 + index * 200}:duration=3", str(music)])
        result = replace_music(root, result, music, index, Event())
        state = read_variant(result)
        assert has_manual_edits(result)
        assert RenderPlan.model_validate(state["plan"]).decisions == edit.plan.decisions
        assert prepare_combined_edit(root, result).plan.decisions == edit.plan.decisions
        verify_output(result, edit.plan.actual_frame_count, 30, expected_audio=True)


def test_refill_preserves_duration_after_episode_still_deletion(tmp_path: Path) -> None:
    root, project, plan, original = fixture_project(tmp_path, "beat_shorts")
    decisions = [plan.decisions[0].model_copy(update={
        "action": "HOLD", "provenance": "user:test", "output_frame_count": 45,
        "source_start_us": index * 4_500_000, "source_end_us": (index + 1) * 4_500_000,
    }) for index in range(2)]
    plan = plan.model_copy(update={"decisions": decisions,
                                   "settings": plan.settings | {"story": "beat_stills"}})
    selected = root / "renders" / "stills.mp4"
    save_variant(selected, project=project, plan=plan)
    save_project(root, project.model_copy(update={"overrides": [Override(
        source_id=project.sources[0].id, start_us=4_500_000,
        end_us=9_000_000, action="DROP",
    )]}))
    result = rebuild_after_cuts(root, selected, [(0, 500_000)], Event(), lambda _: None,
                                encoder="libx264")
    verify_output(result, 90, 30)
    assert prepare_combined_edit(root, result).plan.actual_frame_count == 90


def test_refill_rejects_infeasible_fixed_episode_speed(tmp_path: Path) -> None:
    root, project, _, original = fixture_project(tmp_path)
    save_project(root, project.model_copy(update={"overrides": [Override(
        source_id=project.sources[0].id, start_us=0, end_us=9_000_000,
        action="SET_SPEED", speed=1,
    )]}))
    from sculptcut.combined_edits import prepare_refill_edit

    with pytest.raises(ValueError, match="скорости эпизодов"):
        prepare_refill_edit(root, original, [(0, 500_000)])


def test_bookend_trim_survives_episode_export_reopen_music_and_refill(tmp_path: Path) -> None:
    root, project, _, original = fixture_project(tmp_path, "music_shorts", cover=True)
    save_project(root, project.model_copy(update={"overrides": [Override(
        source_id=project.sources[0].id, start_us=6_000_000,
        end_us=9_000_000, action="DROP",
    )]}))
    edit = prepare_combined_edit(root, original, [(0, 1_000_000), (1_700_000, 1_900_000)])
    result = root / "renders" / "combined.mp4"
    render_combined_edit(root, edit, result, Event(), lambda _: None, encoder="libx264")
    total = output_frame_count(edit.project.settings, 30, edit.plan.actual_frame_count) - 30
    verify_output(result, total, 30)
    assert read_variant(result)["bookend_cuts_us"] == [[0, 1_000_000]]
    again = prepare_combined_edit(root, result)
    assert again.bookend_cuts() == [(0, 1_000_000)]
    next_result = root / "renders" / "again.mp4"
    render_combined_edit(root, again, next_result, Event(), lambda _: None, encoder="libx264")
    verify_output(next_result, total, 30)
    music = tmp_path / "music.wav"
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 "sine=duration=3", str(music)])
    musical = replace_music(root, result, music, 0, Event())
    verify_output(musical, total, 30, expected_audio=True)
    assert prepare_combined_edit(root, musical).bookend_cuts() == [(0, 1_000_000)]
    # Duration refill restores the intro, retaining all source-time exclusions.
    filled = rebuild_after_cuts(root, musical, [(600_000, 700_000)], Event(), lambda _: None,
                               encoder="libx264")
    filled_state = read_variant(filled)
    filled_plan = RenderPlan.model_validate(filled_state["plan"])
    verify_output(filled, 90, 30, expected_audio=True)
    assert all(d.source_end_us <= 6_000_000 for d in filled_plan.decisions if d.output_frame_count)


@pytest.mark.parametrize("order", ["trim_first", "episodes_first"])
def test_real_gui_unifies_unsaved_trims_and_episode_changes(tmp_path: Path, order: str) -> None:
    root, project, _, original = fixture_project(tmp_path)
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    errors: list[str] = []
    try:
        window.folder.setText(str(root))
        window.show()
        window.open_video(original, "Saved")
        for _ in range(150):
            if window.player.duration() == 3000:
                break
            QTest.qWait(20)
        window.output_editor.set_duration(3000)
        window.output_editor.cuts = [(0, 1_000_000)]
        if order == "trim_first":
            def edit_panel() -> None:
                panel = window.episode_panel
                if panel is None:
                    QTimer.singleShot(20, edit_panel)
                    return
                try:
                    assert panel.editor.plan is not None
                    assert panel.editor.plan.decisions[0].provenance == "user:shorts_manual_cut"
                    changed = episode_changes(root, project)
                    panel.editor.edit_saved.emit(project, changed, "Episode")
                    panel.accept()
                except Exception as exc:
                    errors.append(str(exc))
                    panel.reject()
            QTimer.singleShot(0, edit_panel)
            window.show_episodes()
        else:
            episode_changes(root, project)
            def choose() -> None:
                dialog = window.findChild(CutSaveDialog)
                assert dialog is not None
                QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
            QTimer.singleShot(0, choose)
            window.save_output_cuts()
        if window.job is not None:
            loop = QEventLoop()
            timeout = QTimer()
            timeout.setSingleShot(True)
            timeout.timeout.connect(loop.quit)
            window.job.finished.connect(loop.quit)
            timeout.start(60_000)
            loop.exec()
            timeout.stop()
        assert not errors
        assert window.job is None and window.job_succeeded, window.last_error
        result = window.last_result_video
        context = prepare_combined_edit(root, result)
        assert_combined(context.project, context.plan)
        verify_output(result, 60, 30)
        assert not window.output_editor.cuts
        # The selected snapshot remains the basis when reopening the episode window.
        panel = EpisodePanel(root, window, video=result)
        assert panel.editor.plan is not None
        assert_combined(context.project, panel.editor.plan)
        panel.reject()
        assert episode_export(root, result)[1].decisions == context.plan.decisions
    finally:
        if window.job is not None:
            window.job.cancel.set()
            window.job.wait(30_000)
        window.close()
        app.processEvents()
