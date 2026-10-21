"""Live UI language changes preserve user values, projects and machine identifiers."""

import re
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractButton, QApplication, QLabel

from sculptcut.cleanup_dialog import CleanupDialog
from sculptcut.crop_dialog import TimelapseCropDialog, WebcamDialog
from sculptcut.domain import Roi
from sculptcut.feedback_dialog import FeedbackDialog
from sculptcut.i18n import CATALOG, tr, translate
from sculptcut.localization import error_text, stage_text
from sculptcut.new_project_dialog import NewProjectDialog
from sculptcut.presentation_dialog import PresentationDialog
from sculptcut.project import create_empty_project
from sculptcut.ui import DurationSpinBox, MainWindow
from sculptcut.ui_language import install_language


def assert_english(widget, preserved_path: str = "") -> None:
    for child in [widget, *widget.findChildren(QLabel), *widget.findChildren(QAbstractButton)]:
        for property_name in ("text", "toolTip", "accessibleName", "windowTitle"):
            getter = getattr(child, property_name, None)
            if callable(getter):
                text = getter()
                if preserved_path:
                    text = text.replace(preserved_path, "<user path>")
                assert not re.search("[А-Яа-яЁё]", text), (type(child).__name__, getter())


def test_catalog_has_complete_english_and_preserves_template_fields() -> None:
    for source, target in CATALOG.items():
        assert not re.search("[А-Яа-яЁё]", target), source
        assert set(re.findall(r"\{\d+\}", source)) == set(re.findall(r"\{\d+\}", target))
    path = "D:/Мои видео/Музыка.mp4"
    assert translate("Source: " + path, "en") == "Source: " + path
    assert translate("Видео не найдено: " + path, "en") == "Video not found: " + path
    assert translate("Video not found: " + path, "ru") == "Видео не найдено: " + path


def test_live_switch_keeps_user_fields_preset_history_and_persists(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.show()
        app.processEvents()
        window.output_name.setText("Мой дракон")
        window.target.setValue(7.5)
        root = tmp_path / "project"
        create_empty_project(root)
        window.folder.setText(str(root))
        project_data = (root / "project.json").read_bytes()
        original = window.form_draft()
        assert window.language_button.parentWidget() is window.project_actions
        assert window.language_button.text() == "RU"
        assert not hasattr(window, "feedback_button")
        assert not hasattr(window, "language_buttons")
        assert window.language_button.menu() is None
        QTest.mouseClick(window.language_button, Qt.MouseButton.LeftButton)
        app.processEvents()
        assert window.language_button.text() == "EN"
        assert window.create_button.text() == "Create video"
        assert window.locale().name() == "en_US"
        assert QCoreApplication.translate("QPlatformTheme", "Cancel") == "Cancel"
        assert window.output_name.text() == "Мой дракон"
        assert window.target.value() == 7.5
        assert window.form_draft() == original
        assert (root / "project.json").read_bytes() == project_data
        assert window.preset.currentData() == "timelapse"
        assert_english(window)
        window.show_error("Source does not exist: D:/Мои видео/видео.mp4")
        assert window.status.text() == "Source video not found: D:/Мои видео/видео.mp4"
        window.show_error("ValueError: Такая папка или файл уже существует. Измените название.")
        assert window.status.text().startswith("A folder or file")
        window.language_button.click()
        assert window.create_button.text() == "Создать ролик"
        assert window.target.value() == 7.5
        assert window.output_name.text() == "Мой дракон"
        window.language_button.click()
        assert window.recent_settings.value("interface_language") == "en"
        window.close()
        restored = MainWindow()
        try:
            assert restored.create_button.text() == "Create video"
            assert restored.language_button.text() == "EN"
        finally:
            restored.close()
    finally:
        window.close()
        install_language("ru")


@pytest.mark.parametrize("size", [(1120, 700), (1280, 720), (1440, 860)])
def test_english_all_presets_fit_and_have_no_russian_interface_text(size: tuple[int, int]) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.resize(*size)
        window.show()
        window.language_button.click()
        for preset in ("timelapse", "speed", "shorts", "music_shorts", "beat_shorts",
                       "photoshop_shorts", "photoshop_beat_shorts"):
            window.preset.setCurrentIndex(window.preset.findData(preset))
            app.processEvents()
            assert_english(window)
            for scroll in (window.sidebar_scroll, window.viewer_scroll, window.settings_scroll):
                assert scroll.horizontalScrollBar().maximum() == 0, (size, preset)
                assert scroll.verticalScrollBar().maximum() == 0, (size, preset)
    finally:
        window.close()
        install_language("ru")


def test_english_dialogs_and_validation_without_network_or_source_reads(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    dialogs = []
    try:
        window.change_language("en")
        root = tmp_path / "project"
        create_empty_project(root)
        frame = QImage(320, 180, QImage.Format.Format_RGB32)
        frame.fill(0xFF758392)
        source = tmp_path / "source.mp4"
        dialogs = [
            NewProjectDialog(tmp_path, window),
            CleanupDialog(root, (), window),
            FeedbackDialog(window.recent_settings, "timelapse", window,
                           endpoint="https://example.invalid/feedback"),
            PresentationDialog(None, 5, "start", [], window),
            TimelapseCropDialog(source, Roi(x=0, y=0, width=1, height=1), window,
                                initial_frame=frame),
            WebcamDialog(source, None, window, initial_frame=frame),
        ]
        for dialog in dialogs:
            assert_english(dialog, str(tmp_path))
        create = dialogs[0]
        create.name.setText("CON")
        create.refresh()
        assert create.error.text() == "Enter a valid folder name (up to 100 characters)."
        assert not create.create_button.isEnabled()
        duration = DurationSpinBox()
        duration.set_seconds_mode(True)
        assert duration.textFromValue(75) == "1 min 15 sec"
        assert duration.valueFromText("1 min 15 sec") == 75
        assert duration.valueFromText("1 мин 15 сек") == 75
        assert stage_text("Checking timestamps: x.mp4") == "Checking timestamps: x.mp4"
        assert "Cannot access" in error_text("PermissionError: [Errno 13]")
        assert tr("Сведения загружаются…") == "Loading details…"
    finally:
        for dialog in dialogs:
            dialog.close()
        window.close()
        app.processEvents()
        install_language("ru")
