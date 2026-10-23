"""The save choice is explicit, cancellable, and legible in RU/EN."""

from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from sculptcut.cut_save_dialog import CutSaveDialog
from sculptcut.i18n import tr
from sculptcut.ui_language import install_language


@pytest.mark.parametrize("language", ["ru", "en"])
@pytest.mark.parametrize("available", [False, True])
def test_cut_save_dialog_defaults_options_cancel_and_translation(
    tmp_path: Path, language: str, available: bool,
) -> None:
    app = QApplication.instance() or QApplication([])
    install_language(language)
    reason = "" if available else tr("В этом режиме сохранение длительности пока недоступно.")
    dialog = CutSaveDialog(reason)
    try:
        dialog.show()
        app.processEvents()
        assert dialog.isVisible()
        assert dialog.trim.isChecked() and not dialog.keep_duration
        assert dialog.refill.isEnabled() is available
        assert dialog.reason.isVisible() is not available
        if language == "en":
            assert dialog.trim.text() == "Just cut"
            assert dialog.refill.text() == "Cut and keep duration"
            assert dialog.save_button.text() == "Save copy"
            assert "The pace" in dialog.trim_note.text()
        assert dialog.grab().save(str(tmp_path / "save-choice.png"))
        QTest.mouseClick(dialog.refill, Qt.MouseButton.LeftButton)
        assert dialog.keep_duration is available
        QTest.mouseClick(dialog.trim, Qt.MouseButton.LeftButton)
        assert not dialog.keep_duration
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        assert not dialog.isVisible() and dialog.result() == 0
    finally:
        dialog.close()
        app.processEvents()
        install_language("ru")
