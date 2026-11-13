"""Exact additions and trims preserve source coverage, history and unrelated scenes."""

from pathlib import Path
from threading import Event

import pytest
from test_episode_roll import roll_movie as roll_movie
from test_episode_roll import selected_plan
from test_timeline import make_source

from sculptcut.combined_edits import CombinedEdit, apply_episode_edits
from sculptcut.domain import Project
from sculptcut.planning import validate_plan
from sculptcut.source_selection import SourceSelection


def session():
    project = Project(sources=[make_source(0, 0, 36_000_000)])
    plan, _ = selected_plan(project)
    return SourceSelection(
        CombinedEdit(project, project, plan, plan, Path("original.mp4"), [], [], [])
    )


def test_add_only_selected_part_of_gap_grows_duration_and_undo():
    edit = session()
    original = list(edit.plan.decisions)
    edit.select("0", 4_000_000, 5_500_000, 2)
    kept = [d for d in edit.plan.decisions if d.output_frame_count]
    assert [(d.source_start_us, d.source_end_us) for d in kept] == [
        (0, 3_000_000),
        (4_000_000, 5_500_000),
        (12_000_000, 15_000_000),
        (18_000_000, 21_000_000),
    ]
    assert kept[1].output_frame_count == 22
    assert edit.plan.actual_frame_count == 157
    assert kept[0] == original[0] and kept[-1] == original[6]
    assert edit.project.overrides[-1].exact_selection
    assert edit.project.overrides[-1].restore_omitted
    validate_plan(edit.project, edit.plan)
    edit.undo()
    assert not edit.changed and edit.plan.decisions == original
    edit.undo(redo=True)
    assert edit.changed and edit.plan.actual_frame_count == 157


def test_resize_trims_tail_extends_head_without_touching_neighbours():
    edit = session()
    edit.resize(4, 11_000_000, 14_000_000, 2)
    kept = [d for d in edit.plan.decisions if d.output_frame_count]
    assert sum(d.output_frame_count for d in kept[1:-1]) == 45
    assert kept[0] == edit.base.decisions[0] and kept[-1] == edit.base.decisions[6]
    assert all(
        d.action == "DROP"
        for d in edit.plan.decisions
        if d.source_start_us >= 14_000_000 and d.source_end_us <= 18_000_000
    )
    with pytest.raises(ValueError, match="соседней"):
        edit.resize(0, 0, 12_000_000, 2)


def test_apply_saved_rules_and_remove_rule_restore_neutral_layer():
    edit = session()
    edit.select("0", 4_000_000, 5_500_000, 2)
    assert apply_episode_edits(edit.project, edit.base).decisions == edit.plan.decisions
    assert apply_episode_edits(edit.original.project, edit.base).decisions == edit.base.decisions


def test_fit_keeps_every_manually_chosen_source_window():
    edit = session()
    edit.select("0", 4_000_000, 8_000_000, 1)
    windows = [(d.source_id, d.source_start_us, d.source_end_us) for d in edit.plan.decisions]
    edit.fit_duration()
    assert edit.plan.actual_frame_count == edit.target == 135
    assert [
        (d.source_id, d.source_start_us, d.source_end_us) for d in edit.plan.decisions
    ] == windows
    assert any(d.source_start_us == 4_000_000 and d.output_frame_count for d in edit.plan.decisions)
    assert edit.original.leading == []


def test_source_bounds_all_dropped_and_hold_rejected_without_mutation():
    edit = session()
    before = edit.plan
    for a, b in ((-1, 100), (0, 37_000_000), (100, 100)):
        with pytest.raises(ValueError):
            edit.select("0", a, b, 2)
    with pytest.raises(ValueError):
        edit.select("0", 0, 36_000_000, None)
    assert edit.plan == before and not edit.undo_stack
    plan = before.model_copy(
        update={
            "decisions": [
                d.model_copy(update={"action": "HOLD", "provenance": "user:final_tail"})
                if i == 6
                else d
                for i, d in enumerate(before.decisions)
            ]
        }
    )
    edit.plan = plan
    with pytest.raises(ValueError):
        edit.select("0", 18_000_000, 20_000_000, 2)
    assert edit.plan == plan


def test_real_preview_and_final_reopen_preserve_exact_addition_music_cover_and_trim(roll_movie):
    from sculptcut.combined_edits import prepare_combined_edit, render_combined_edit
    from sculptcut.intro import output_frame_count
    from sculptcut.project import save_project
    from sculptcut.rendering import verify_output
    from sculptcut.variants import read_variant

    root, project, plan, episodes, video = roll_movie
    before = video.read_bytes()
    edit = prepare_combined_edit(
        root, video, [(1_000_000, 1_200_000)], apply_current=False, exact_variant=True
    )
    session = SourceSelection(edit)
    session.select(project.sources[0].id, 4_000_000, 5_500_000, 2)
    final_edit = session.export_edit()
    preview = root / "previews/selection.mp4"
    render_combined_edit(
        root,
        final_edit,
        preview,
        Event(),
        lambda value: None,
        preview=True,
        encoder="libx264",
        operation="source_selection",
    )
    save_project(root, project.model_copy(update={"overrides": session.project.overrides}))
    expected = output_frame_count(project.settings, 30, session.plan.actual_frame_count)
    verify_output(preview, expected, 30, expected_audio=True)
    reopened = prepare_combined_edit(root, preview)
    assert reopened.plan.decisions == session.plan.decisions
    assert reopened.plan.actual_frame_count == session.plan.actual_frame_count
    output = root / "renders/final-selection.mp4"
    render_combined_edit(root, reopened, output, Event(), lambda value: None, encoder="libx264")
    verify_output(output, expected, 30, expected_audio=True)
    saved = read_variant(output)
    assert any(o["exact_selection"] for o in saved["project"]["overrides"])
    assert saved["plan"]["decisions"] == read_variant(preview)["plan"]["decisions"]
    assert video.read_bytes() == before


def test_gui_drag_partial_gap_preview_audio_undo_and_cancel_leave_project_untouched(
    roll_movie, tmp_path
):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QWidget
    from test_episode_panel import wait_until

    from sculptcut.media import probe
    from sculptcut.source_selection_dialog import SourceSelectionDialog

    app = QApplication.instance() or QApplication([])
    root, project, plan, episodes, video = roll_movie
    protected = {
        p: p.read_bytes() for p in (video, root / "project.json", root / "render_plan.json")
    }
    owner = QWidget()
    dialog = SourceSelectionDialog(root, video, owner, compact=False)
    try:
        dialog.show()
        wait_until(app, lambda: not dialog.loading)
        assert dialog.montage.count() == 3
        timeline = dialog.timeline
        dialog.select_range(4_000_000, 6_000_000)
        timeline.set_view(0, 12_000_000)
        QTest.mousePress(
            timeline, Qt.MouseButton.LeftButton, pos=QPoint(round(timeline.x_at(6_000_000)), 45)
        )
        QTest.mouseMove(timeline, QPoint(round(timeline.x_at(5_500_000)), 45))
        QTest.mouseRelease(
            timeline, Qt.MouseButton.LeftButton, pos=QPoint(round(timeline.x_at(5_500_000)), 45)
        )
        assert abs(dialog.start.value() - 4) < 0.03 and abs(dialog.end.value() - 5.5) < 0.03
        dialog.speed.setValue(2)
        dialog.keep.click()
        assert dialog.session.changed and dialog.montage.count() == 4
        dialog.undo.click()
        assert not dialog.session.changed
        dialog.redo.click()
        assert dialog.session.changed
        assert abs(dialog.start.value() - 4) < 0.03
        wait_until(app, lambda: dialog.preparer.worker is None)
        dialog.context.click()
        try:
            wait_until(app, lambda: dialog.context_ready)
        except AssertionError:
            pytest.fail(dialog.message.text())
        path = Path(dialog.playback.player.source().toLocalFile())
        assert probe(path).audio_streams
        wait_until(app, lambda: dialog.playback.is_advancing())
        assert not dialog.playback.audio.isMuted()
        dialog.play.click()
        assert not dialog.playback.want_play
        assert dialog.playback.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState
        dialog.play.click()
        assert dialog.playback.want_play
        dialog.grab().save(str(tmp_path / "selection-editor.png"))
        dialog.context.click()
        assert not dialog.context_ready and dialog.playback.audio.isMuted()
        assert {p: p.read_bytes() for p in protected} == protected
        dialog.reject()
        wait_until(app, lambda: dialog.preparer.cleaned)
    finally:
        dialog.shutdown()
        owner.close()
        app.processEvents()


def test_main_window_preview_apply_then_original_export_keeps_manual_bounds(
    roll_movie, monkeypatch
):
    from PySide6.QtWidgets import QApplication
    from test_episode_panel import wait_until

    from sculptcut.intro import output_frame_count
    from sculptcut.project import load_project
    from sculptcut.rendering import verify_output
    from sculptcut.ui import MainWindow
    from sculptcut.variants import read_variant

    app = QApplication.instance() or QApplication([])
    root, project, plan, episodes, video = roll_movie
    window = MainWindow()
    errors = []
    monkeypatch.setattr(window, "show_error", errors.append)
    try:
        window.folder.setText(str(root))
        window.show()
        window.open_video(video, "test")
        window.player.pause()
        assert window.selection_button.isEnabled() and not window.roll_button.isVisible()
        original_viewer = window.viewer_scroll.widget()
        window.cut_button.click()

        dialog = window.selection_dialog
        wait_until(app, lambda: not dialog.loading)
        assert not dialog.isWindow()
        assert window.viewer_scroll.widget() is original_viewer
        assert not dialog.picture.isVisible() and not dialog.montage.isVisible()
        assert window.video.isVisible()
        dialog.select_range(4_000_000, 5_500_000)
        dialog.speed.setValue(2)
        dialog.keep.click()
        dialog.apply.click()
        wait_until(app, lambda: window.job is None, 25)
        assert not errors, errors
        wait_until(app, lambda: window.selection_dialog is not None
                   and not window.selection_dialog.loading)
        assert window.viewer_scroll.widget() is original_viewer
        previews = list((root / "previews").glob("*selection*.mp4"))
        assert len(previews) == 1
        state = read_variant(previews[0])
        assert state["operation"] == "source_selection" and state["plan"]["settings"]["draft"] == 1
        assert load_project(root).overrides[-1].exact_selection
        assert any(
            d["source_start_us"] == 4_000_000
            and d["source_end_us"] == 5_500_000
            and d["output_frame_count"] > 0
            for d in state["plan"]["decisions"]
        )
        assert window.export_button.isEnabled()
        window.export_button.click()
        wait_until(app, lambda: window.job is None, 25)
        assert not errors, errors
        finals = [p for p in (root / "renders").glob("*.mp4") if p != video]
        assert len(finals) == 1
        saved = read_variant(finals[0])
        assert saved["plan"]["decisions"] == state["plan"]["decisions"]
        count = state["plan"]["actual_frame_count"]
        assert output_frame_count(project.settings, 30, count) == round(
            project.settings.target_seconds*30)
        verify_output(
            finals[0], output_frame_count(project.settings, 30, count), 30, expected_audio=True
        )
    finally:
        window._allow_close = True
        window.close()
        app.processEvents()


def test_context_preview_audio_fade_loop_and_hold_have_exact_frame_budget(roll_movie, tmp_path):
    import cv2
    import numpy as np

    from sculptcut.episode_context_preview import PreviewAudio, PreviewScene, prepare_clip
    from sculptcut.media import discover, run_process
    from sculptcut.rendering import verify_output

    root, project, plan, episodes, video = roll_movie
    path = project.sources[0].path
    hold = plan.decisions[6].model_copy(
        update={
            "action": "HOLD",
            "source_end_us": 18_001_000,
            "output_frame_count": 60,
            "provenance": "user:final_tail",
        }
    )
    scenes = (
        PreviewScene(0, path, 0, 3000, 30, plan.decisions[0]),
        PreviewScene(1, path, 18000, 18001, 60, hold),
    )
    cache = tmp_path / "short-preview"
    cache.mkdir()
    clip = prepare_clip(
        cache,
        scenes,
        30,
        (320, 180),
        Event(),
        PreviewAudio(project.settings.music_path, 4, 7, True, 0),
    )
    verify_output(clip.path, 90, 30, expected_audio=True)
    pcm = tmp_path / "audio.f32"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-i",
            str(clip.path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-f",
            "f32le",
            str(pcm),
        ]
    )
    samples = np.fromfile(pcm, dtype="<f4")
    early = np.sqrt(np.mean(samples[4800:14400] ** 2))
    late = np.sqrt(np.mean(samples[134400:141600] ** 2))
    assert early > 0 and late < early * 0.03
    cap = cv2.VideoCapture(str(clip.path))
    images = []
    for ms in (1500, 2500):
        cap.set(cv2.CAP_PROP_POS_MSEC, ms)
        ok, image = cap.read()
        assert ok
        images.append(image.astype(float))
    cap.release()
    assert np.mean(np.abs(images[0] - images[1])) < 1


def test_exact_manual_range_survives_auto_selection_without_work_search():
    from sculptcut.overrides import apply_overrides
    from sculptcut.restored_work import prepare_restored_work

    edit = session()
    edit.select("0", 4_000_000, 5_500_000, 2)
    project, base, bounded = prepare_restored_work(None, edit.project, edit.base)
    assert project == edit.project and base == edit.base and not bounded
    base = base.model_copy(update={"settings": base.settings | {"story": "phrased_timelapse"}})
    fresh = apply_overrides(project, base, manual=False)
    assert any(
        d.source_start_us == 4_000_000
        and d.source_end_us == 5_500_000
        and d.output_frame_count == 22
        for d in fresh.decisions
    )
