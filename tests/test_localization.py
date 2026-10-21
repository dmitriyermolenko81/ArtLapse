import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication, QComboBox, QLabel, QPushButton, QScrollArea

from sculptcut.localization import error_text, stage_text
from sculptcut.ui import MainWindow


def test_russian_ui_preserves_machine_values_and_qt_dialogs() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.resize(960, 700)
        window.show()
        app.processEvents()
        assert window.target.height() >= 36
        assert window.create_button.height() >= 36
        assert window.target.value() == 10
        assert window.target.suffix() == " мин"
        scroll = window.findChild(QScrollArea)
        assert scroll is not None
        assert scroll.horizontalScrollBar().maximum() == 0
        assert not hasattr(window, "preview_button")
        assert window.create_button.isVisible()
        assert window.create_button.text() == "Создать ролик"
        labels = [w.text() for w in window.findChildren(QLabel)]
        buttons = [w.text() for w in window.findChildren(QPushButton)]
        assert not any("ОБЛАСТЬ МОДЕЛИ" in text or "ПРОВЕРКА" in text for text in labels)
        assert "Анализ и план" not in buttons
        assert window.locale().name() == "ru_RU"
        assert QCoreApplication.translate("QPlatformTheme", "Cancel") == "Отмена"
        assert set(window.findChildren(QComboBox)) == {
            window.variants,
        }
        assert window.shorts_layout.buttons["square"].toolTip() == "Квадрат 1:1 с полями"
        assert window.shorts_layout.findData("square") == 1
        assert window.variants.placeholderText() == "Выберите готовый ролик"
        assert "Применить" in buttons
        assert any(w.accessibleName() == "Применить настройки выбранной версии"
                   for w in window.findChildren(QPushButton))
        assert not window.versions_panel.isVisible()
        window.target.setValue(7.5)
        window.preset.setCurrentIndex(window.preset.findData("shorts"))
        assert window.target.value() == 60
        assert window.target.isEnabled()
        assert window.target.seconds_mode and window.target.suffix() == ""
        assert window.output_suffix.text() == "_shorts.mp4"
        window.preset.setCurrentIndex(window.preset.findData("timelapse"))
        assert window.target.isEnabled()
        assert window.target.value() == 7.5
        assert window.output_suffix.text() == "_timelapse.mp4"
        window.preset.setCurrentIndex(window.preset.findData("speed"))
        assert window.target.isEnabled() and window.target.value() == 7.5
        assert window.output_suffix.text() == "_speed.mp4"
        window.show_error("PermissionError: [Errno 13] private path")
        assert "Нет доступа" in window.status.text()
        assert "private path" in window.status.toolTip()
        window.show_error("Cancelled: Media operation cancelled")
        assert "Обработка отменена" in window.status.text()
    finally:
        window.close()
        app.processEvents()


def test_localized_status_keeps_source_paths_and_error_diagnostics() -> None:
    path = "D:/Мои видео/запись.mp4"
    assert stage_text("Checking timestamps: " + path) == "Проверка временных меток: " + path
    assert error_text("MediaError: Source does not exist: " + path).endswith(path)
    assert "Не удалось" in error_text("Unexpected vendor codec failure")
    assert "Область модели" in error_text("1 validation error: ROI extends beyond image")


def test_blank_name_is_visible_and_error_details_are_clickable(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from PySide6.QtWidgets import QMessageBox

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.show()
        window.folder.setText(str(tmp_path / "project"))
        window.set_sources([tmp_path / "input.mp4"])
        monkeypatch.setattr(window, "find_missing_sources", lambda callback: False)
        window.output_name.clear()
        window.start_action("create")
        app.processEvents()
        assert window.job is None
        assert window.status.text() == "Введите имя файла перед созданием ролика."
        assert window.output_name.hasFocus()
        assert window.error_details_button.isVisible()
        diagnostics = []
        monkeypatch.setattr(QMessageBox, "exec", lambda dialog: diagnostics.append(
            (dialog.text(), dialog.detailedText(), dialog.windowTitle())
        ) or 0)
        window.show_error("Unexpected vendor codec failure\nfull diagnostic")
        window.error_details_button.click()
        assert diagnostics[0][1] == window.last_error
        assert diagnostics[0][2] == "Подробности ошибки"
        assert diagnostics[0][0] == "Не удалось выполнить операцию."
        assert "подсказке" not in window.status.text()
    finally:
        window.close()
        app.processEvents()
