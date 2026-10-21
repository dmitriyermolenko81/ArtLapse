from collections.abc import Iterator
from pathlib import Path
from threading import Event
from time import monotonic, sleep

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from sculptcut.crop_dialog import TimelapseCropDialog, WebcamDialog
from sculptcut.edit_history import EditHistory
from sculptcut.media import discover, run_process
from sculptcut.output_cuts import cut_output
from sculptcut.project import create_project, load_project
from sculptcut.rendering import verify_output
from sculptcut.ui import Job, MainWindow


def test_order_branch_noop_and_bounded_history() -> None:
    history = EditHistory(limit=2)
    values: list[int] = []
    for value in range(1, 4):
        history.record(str(value), value - 1, value, values.append)
    history.undo()
    history.undo()
    history.undo()
    assert values == [2, 1]
    assert not history.undo_label and history.redo_label == "2"
    history.record("no change", 1, 1, values.append)
    assert history.redo_label == "2"
    history.redo()
    history.record("branch", 2, 7, values.append)
    assert not history.redo_label
    history.undo()
    history.redo()
    assert values[-3:] == [2, 2, 7]
    history.clear()
    assert not history.undo_label and not history.redo_label


def test_failed_replay_preserves_cursor() -> None:
    history = EditHistory()

    def fail(value: int) -> None:
        raise OSError("disk unavailable")

    history.record("change", 0, 1, fail)
    with pytest.raises(OSError):
        history.undo()
    assert history.undo_label == "change" and not history.redo_label
    assert history.position == 1


@pytest.fixture
def editing_window(tmp_path: Path) -> Iterator[tuple[QApplication, MainWindow, Path]]:
    app = QApplication.instance() or QApplication([])
    source = tmp_path / "исходник.mp4"
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 "testsrc2=s=320x180:r=30:d=4", "-c:v", "libx264", str(source)])
    root = tmp_path / "project"
    create_project(root, [source])
    window = MainWindow()
    window.open_project_folder(root)
    window.show()
    window.activateWindow()
    window.open_video(source, "Тест истории")
    deadline = monotonic() + 10
    while not window.player.duration() and monotonic() < deadline:
        QTest.qWait(20)
    assert window.player.duration() == 4000
    window.player.pause()
    window.output_editor.set_duration(4000)
    window.output_editor.show()
    try:
        yield app, window, source
    finally:
        from sculptcut.source_cards import _active_source_loaders

        deadline = monotonic() + 10
        while _active_source_loaders and monotonic() < deadline:
            app.processEvents()
            sleep(0.01)
        assert not _active_source_loaders
        window.close()
        app.processEvents()


def change_duration(window: MainWindow, minutes: int) -> None:
    window.duration_card.minutes.setValue(minutes)
    window.duration_card.minutes.editingFinished.emit()


def cut(window: MainWindow) -> None:
    window.output_editor.start.setValue(1)
    window.output_editor.end.setValue(2)
    window.output_editor.remove_button.click()


def test_mixed_edits_shortcuts_redo_and_real_export(
    editing_window: tuple[QApplication, MainWindow, Path], tmp_path: Path,
) -> None:
    app, window, source = editing_window
    history = window.history_controller
    original = source.read_bytes()
    initial_duration = window.target.value()
    cut(window)
    change_duration(window, 3)

    def keep_frame() -> None:
        dialog = QApplication.activeModalWidget()
        if isinstance(dialog, TimelapseCropDialog):
            dialog.reject()
        else:
            QTimer.singleShot(20, keep_frame)

    QTimer.singleShot(20, keep_frame)
    window.frame_mode.buttons["viewport"].click()
    assert window.edit_history.position == 3
    assert window.output_editor.edited_duration == 3_000_000
    window.play_button.setFocus()
    QTest.keyClick(window, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert window.frame_mode.currentData() == "full"
    history.undo_button.click()
    assert window.target.value() == initial_duration
    window.output_editor.undo_button.click()
    assert not window.output_editor.cuts
    assert window.output_editor.edited_duration == 4_000_000
    assert not history.undo_button.isEnabled()
    QTest.keyClick(window, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClick(window, Qt.Key.Key_Z,
                   Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    history.redo_button.click()
    assert window.target.value() == 3
    assert window.frame_mode.currentData() == "viewport"
    assert window.output_editor.cuts == [(1_000_000, 2_000_000)]
    result = cut_output(source, window.output_editor.cuts, Event(), lambda _: None)
    verify_output(result, 90, 30)
    assert source.read_bytes() == original
    assert not window.last_error, window.last_error
    app.processEvents()


def test_duration_presets_cross_modes_and_new_branch(
    editing_window: tuple[QApplication, MainWindow, Path],
) -> None:
    _, window, _ = editing_window
    h = window.history_controller
    change_duration(window, 4)
    window.preset.buttons["speed"].click()
    window.shorts_mode.click()
    assert window.duration_in_seconds
    window.duration_card.seconds.setValue(12)
    window.duration_card.seconds.editingFinished.emit()
    assert window.target.value() == 72
    h.undo()
    assert window.target.value() == 60
    h.undo()
    assert not window.duration_in_seconds
    assert window.preset.currentData() == "speed" and window.target.value() == 4
    h.redo()
    assert window.duration_in_seconds and window.target.value() == 60
    change_duration(window, 2)
    assert not h.redo_button.isEnabled()
    assert not window.last_error


def test_modal_crop_cancel_and_accept_are_single_actions(
    editing_window: tuple[QApplication, MainWindow, Path],
) -> None:
    app, window, source = editing_window
    deadline = monotonic() + 5
    while str(source) not in window.source_media_cache and monotonic() < deadline:
        app.processEvents()
        sleep(0.01)
    assert str(source) in window.source_media_cache
    h = window.history_controller
    original = window.crop_roi
    steps: list[int] = []
    window.frame_mode.setCurrentIndex(window.frame_mode.findData("viewport"))

    def edit_dialog(accept: bool) -> None:
        dialog = QApplication.activeModalWidget()
        if not isinstance(dialog, TimelapseCropDialog) or dialog.canvas is None:
            QTimer.singleShot(20, lambda: edit_dialog(accept))
            return
        canvas = dialog.canvas
        assert not hasattr(dialog, "loader")
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))
        for point in (QPoint(100, 70), QPoint(160, 100), QPoint(240, 140)):
            QTest.mouseMove(canvas, point)
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(240, 140))
        steps.append(1)
        if accept:
            dialog.accept()
        else:
            dialog.reject()

    QTimer.singleShot(20, lambda: edit_dialog(False))
    window.frame_mode.buttons["viewport"].click()
    assert window.crop_roi == original and window.edit_history.position == 0
    QTimer.singleShot(20, lambda: edit_dialog(True))
    window.frame_mode.buttons["viewport"].click()
    revised = window.crop_roi
    assert len(steps) == 2 and revised != original
    assert window.edit_history.position == 1
    h.undo()
    assert window.crop_roi == original and not window.crop_customized
    h.redo()
    assert window.crop_roi == revised and window.crop_customized
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not window.findChildren(TimelapseCropDialog)


def test_history_survives_save_and_switching_video(
    editing_window: tuple[QApplication, MainWindow, Path], tmp_path: Path,
) -> None:
    _, window, source = editing_window
    cut(window)
    other = tmp_path / "другое.mp4"
    other.write_bytes(source.read_bytes())
    window.open_video(other, "Другое видео")
    assert not window.output_editor.cuts
    window.history_controller.undo()
    assert Path(window.player.source().toLocalFile()) == source
    window.history_controller.redo()
    assert window.output_editor.cuts == [(1_000_000, 2_000_000)]
    change_duration(window, 3)
    window.save_current_project()
    deadline = monotonic() + 15
    while window.job is not None and monotonic() < deadline:
        QApplication.processEvents()
        sleep(0.01)
    assert window.job is None and not window.last_error
    assert window.edit_history.position == 2
    assert load_project(Path(window.folder.text())).settings.target_seconds == 180
    window.history_controller.undo()
    assert window.target.value() == 10
    window.save_current_project()
    deadline = monotonic() + 15
    while window.job is not None and monotonic() < deadline:
        QApplication.processEvents()
        sleep(0.01)
    assert window.job is None and not window.last_error
    assert load_project(Path(window.folder.text())).settings.target_seconds == 600


def test_busy_and_project_boundaries_and_native_text_undo(
    editing_window: tuple[QApplication, MainWindow, Path], tmp_path: Path,
) -> None:
    _, window, _ = editing_window
    change_duration(window, 3)
    h = window.history_controller
    window.output_name.setFocus()
    QTest.keyClicks(window.output_name, "test")
    QTest.keyClick(window.output_name, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert window.output_name.text() == "" and window.edit_history.position == 1
    window.job = Job(lambda *_: "")
    h.refresh()
    assert not h.undo_action.isEnabled() and not h.undo_button.isEnabled()
    h.undo()
    assert window.target.value() == 3
    window.job.deleteLater()
    window.job = None
    h.refresh()
    window.open_project_folder(tmp_path / "new-project")
    assert not window.edit_history.entries
    change_duration(window, 5)
    window.sources.selectAll()
    window.remove_sources()
    assert not window.edit_history.entries


def test_replaced_video_refuses_undo_without_losing_history(
    editing_window: tuple[QApplication, MainWindow, Path],
) -> None:
    _, window, source = editing_window
    cut(window)
    source.write_bytes(source.read_bytes() + b"changed")
    window.history_controller.undo()
    assert window.edit_history.position == 1
    assert window.output_editor.cuts == [(1_000_000, 2_000_000)]
    assert "Видео изменилось" in window.last_error


def test_committed_duration_uses_global_keyboard_undo(
    editing_window: tuple[QApplication, MainWindow, Path],
) -> None:
    _, window, _ = editing_window
    field = window.duration_card.minutes
    QTest.mouseClick(field, Qt.MouseButton.LeftButton)
    field.selectAll()
    QTest.keyClicks(field, "3")
    QTest.keyClick(field, Qt.Key.Key_Return)
    assert window.target.value() == 3 and window.edit_history.position == 1
    QTest.keyClick(field, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert window.target.value() == 10 and window.edit_history.position == 0


@pytest.mark.parametrize("kind", ["square", "webcam"])
def test_source_frames_restore_roi_and_enabled_state(
    editing_window: tuple[QApplication, MainWindow, Path], kind: str,
) -> None:
    _, window, source = editing_window

    def accept_frame() -> None:
        dialog = QApplication.activeModalWidget()
        if not isinstance(dialog, (TimelapseCropDialog, WebcamDialog)) or dialog.canvas is None:
            QTimer.singleShot(20, accept_frame)
            return
        canvas = dialog.canvas
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))
        QTest.mouseMove(canvas, QPoint(130, 100))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(130, 100))
        dialog.accept()

    QTimer.singleShot(20, accept_frame)
    if kind == "square":
        assert window.choose_square_area()
        roi = window.square_rois[source.resolve()]
    else:
        assert window.choose_webcam_areas(force=True)
        roi = window.webcam_rois[source.resolve()]
        assert window.webcam_enabled.isChecked()
    assert window.edit_history.position == 1
    window.history_controller.undo()
    assert not window.square_rois and not window.webcam_rois
    assert not window.webcam_enabled.isChecked()
    window.history_controller.redo()
    if kind == "square":
        assert window.square_rois[source.resolve()] == roi
    else:
        assert window.webcam_rois[source.resolve()] == roi
        assert window.webcam_enabled.isChecked()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not window.findChildren(TimelapseCropDialog)
    assert not window.findChildren(WebcamDialog)


def test_keyboard_undo_mode_while_hours_field_has_focus(
    editing_window: tuple[QApplication, MainWindow, Path],
) -> None:
    app, window, _ = editing_window
    window.shorts_mode.click()
    window.timelapse_mode.click()
    assert window.edit_history.position == 2
    window.duration_card.hours.setFocus()
    app.processEvents()
    QTest.keyClick(window.duration_card.hours, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert window.duration_in_seconds and window.target.value() == 60
    assert window.edit_history.position == 1
    assert len(window.edit_history.entries) == 2
    window.history_controller.redo()
    assert not window.duration_in_seconds and window.target.value() == 10


def test_replay_ignores_notifications_from_restored_widgets() -> None:
    history = EditHistory()
    values: list[int] = []

    def restore(value: int) -> None:
        values.append(value)
        history.record("widget signal", 7, 8, values.append)
        history.undo()
        history.redo()

    history.record("user edit", 0, 1, restore)
    history.undo()
    assert history.position == 0 and history.redo_label == "user edit"
    history.redo()
    assert history.position == 1 and len(history.entries) == 1
    assert values == [0, 1]
