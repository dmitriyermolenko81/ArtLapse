from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from test_episode_panel import wait_until
from test_project_cleanup import project_root as project_root
from test_variant_deletion import wait_job

from sculptcut.cut_save_dialog import CutSaveDialog
from sculptcut.rendering import verify_output
from sculptcut.ui import MainWindow


@pytest.mark.parametrize("size", [(1120, 700), (1440, 860)])
@pytest.mark.parametrize("maximized", [False, True])
def test_inline_trim_expands_up_into_preview_and_keeps_window_geometry(
    project_root: Path, size: tuple[int, int], maximized: bool,
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.resize(*size)
        window.showMaximized() if maximized else window.show()
        window.open_project_folder(project_root)
        wait_until(app, lambda: not window.video_frame_image.isNull())
        window.player.pause()
        QTest.qWait(100)

        def geometry():
            return (window.frameGeometry(), window.windowState(), [
                (control.mapTo(window, QPoint()), control.size()) for control in (
                    window.cut_button, window.versions_panel, window.settings_scroll,
                    window.duration_card, window.create_button,
                )
            ])

        before = geometry()
        preview = window.video.geometry()
        seek = window.seek.mapTo(window, QPoint())
        window.toggle_output_editor()
        QTest.qWait(100)
        assert geometry() == before
        editor = window.output_editor
        assert editor.isVisible() and editor.window() is window
        assert window.video.y() == preview.y() and window.video.width() == preview.width()
        assert preview.height() - window.video.height() == editor.height()
        assert window.seek.mapTo(window, QPoint()).y() == seek.y() - editor.height()
        assert editor.geometry().bottom() < window.cut_button.geometry().top()
        assert window.viewer_scroll.verticalScrollBar().maximum() == 0
        assert window.versions_panel.isVisible() and not window.versions_panel.isEnabled()
        assert editor.parentWidget() is window.cut_section
        window.output_editor.start.setValue(0.05)
        window.output_editor.end.setValue(0.15)
        window.output_editor.add_cut()
        cuts = list(window.output_editor.cuts)
        assert cuts
        QTest.keyClick(editor, Qt.Key.Key_Escape)
        QTest.qWait(50)
        assert not editor.isVisible() and not window.cut_button.isChecked()
        assert window.output_editor.cuts == cuts and geometry() == before
        assert window.video.geometry() == preview
        window.toggle_output_editor()
        QTest.qWait(50)
        assert window.output_editor.cuts == cuts and geometry() == before
        window.cut_button.click()
        QTest.qWait(50)
        assert window.versions_panel.isEnabled() and not window.cut_button.isChecked()
        assert geometry() == before
    finally:
        window.close()
        app.processEvents()


def test_inline_trim_keeps_shortcuts_save_and_source_switch(project_root: Path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    root = project_root
    original = root / "renders/готовый.mp4"
    source = original.read_bytes()
    try:
        window.show()
        window.open_project_folder(root)
        wait_until(app, lambda: window.player.duration() == 300)
        window.toggle_output_editor()
        editor = window.output_editor
        editor.start.setValue(0.05)
        editor.end.setValue(0.15)
        editor.add_cut()
        cuts = list(editor.cuts)
        window.activateWindow()
        QTest.qWait(100)
        QTest.keyClick(editor, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert not editor.cuts
        QTest.keyClick(editor, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
        assert editor.cuts == cuts
        errors = []

        def choose(save: bool) -> None:
            dialog = window.findChild(CutSaveDialog)
            try:
                assert dialog is not None and dialog.isVisible()
                (dialog.save_button if save else dialog.cancel_button).click()
            except Exception as exc:
                errors.append(str(exc))
                if dialog:
                    dialog.reject()

        QTimer.singleShot(0, lambda: choose(False))
        editor.save_button.click()
        assert not errors and editor.isVisible() and editor.cuts == cuts
        QTimer.singleShot(0, lambda: choose(True))
        editor.save_button.click()
        assert not errors
        wait_job(window)
        assert not editor.isVisible() and not window.cut_button.isChecked()
        assert window.versions_panel.isEnabled()
        assert original.read_bytes() == source
        result = window.last_result_video
        verify_output(result, 2, 10)
        window.toggle_output_editor()
        assert editor.isVisible()
        # Reopening a cut variant switches playback to its original parent.
        # Select a different file to exercise actual source-change dismissal.
        window.open_video(result, "Сохранённый вариант")
        assert not editor.isVisible()
    finally:
        if window.job is not None:
            wait_job(window)
        window.close()
        assert not window.output_editor.isVisible()
        app.processEvents()
