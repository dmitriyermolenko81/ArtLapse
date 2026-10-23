"""ArtLapse branding keeps existing preferences, projects and launch scripts usable."""

import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtGui import QIcon, QImage
from PySide6.QtWidgets import QApplication, QLabel

from sculptcut.cli import build_parser
from sculptcut.feedback_dialog import FeedbackDialog
from sculptcut.project import create_empty_project, load_project
from sculptcut.project_deletion import project_deletion_target
from sculptcut.runtime_paths import data_directory, environment_value
from sculptcut.ui import MainWindow


def test_visible_brand_and_selected_icon_assets() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    feedback = FeedbackDialog(
        window.recent_settings, "timelapse", window, endpoint="https://example.test/feedback",
    )
    try:
        window.show()
        app.processEvents()
        assert window.windowTitle() == "ArtLapse"
        title = window.findChild(QLabel, "title")
        assert title is not None and title.text() == "ArtLapse"
        assert "ArtLapse" in feedback.windowTitle()
        assert "SculptCut" not in build_parser().format_help()
        assert build_parser().prog == "artlapse"
        root = Path(__file__).resolve().parents[1]
        icon = root / "src/sculptcut/assets/logo.ico"
        assert icon.read_bytes() == (root / "packaging/ArtLapse.ico").read_bytes()
        expected = QIcon(str(icon)).pixmap(32, 32).toImage()
        assert not expected.isNull()
        assert window.windowIcon().pixmap(32, 32).toImage() == expected
        png = QImage(str(root / "src/sculptcut/assets/logo.png"))
        assert not png.isNull() and png.hasAlphaChannel()
        brand = window.findChild(QLabel, "brandIcon")
        assert brand is not None
        header = brand.pixmap().toImage()
        for image in (png, expected, header):
            assert image.hasAlphaChannel()
            assert image.pixelColor(0, 0).alpha() == 0
            # Both the removed background and the Play cutout must stay transparent.
            assert image.pixelColor(image.width() // 2, image.height() // 2).alpha() <= 4
        assert {16, 24, 32, 48, 64, 128, 256} <= {
            size.width() for size in window.windowIcon().availableSizes()
        }
    finally:
        feedback.close()
        window.close()
        app.processEvents()


@pytest.mark.parametrize("current_language", [None, "ru"])
def test_legacy_preferences_copy_once_without_changing_old_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, current_language: str | None,
) -> None:
    legacy_file, current_file = tmp_path / "SculptCut.ini", tmp_path / "ArtLapse.ini"
    legacy = QSettings(str(legacy_file), QSettings.Format.IniFormat)
    legacy.setValue("interface_language", "en")
    legacy.setValue("migration_marker", "existing preferences")
    legacy.sync()
    old_bytes = legacy_file.read_bytes()
    if current_language:
        current = QSettings(str(current_file), QSettings.Format.IniFormat)
        current.setValue("interface_language", current_language)
        current.sync()

    def settings(organization: str, application: str) -> QSettings:
        path = legacy_file if organization == "SculptCut" else current_file
        return QSettings(str(path), QSettings.Format.IniFormat)

    monkeypatch.setattr("sculptcut.ui.QSettings", settings)
    monkeypatch.setattr("sculptcut.ui.data_directory", lambda: None)
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        assert window.recent_settings.value("interface_language") == (current_language or "en")
        if current_language is None:
            assert window.recent_settings.value("migration_marker") == "existing preferences"
        else:
            assert not window.recent_settings.contains("migration_marker")
        assert legacy_file.read_bytes() == old_bytes
    finally:
        window.close()
        app.processEvents()
    assert legacy_file.read_bytes() == old_bytes


def test_new_environment_and_legacy_data_folder_are_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "installed/ArtLapse.exe"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    for key in ("ARTLAPSE_DATA_DIR", "SCULPTCUT_DATA_DIR"):
        monkeypatch.delenv(key, raising=False)
    assert data_directory() == tmp_path / "ArtLapse"
    legacy = tmp_path / "SculptCut"
    legacy.mkdir()
    (legacy / "settings.ini").write_text("preserve", encoding="utf-8")
    assert data_directory() == legacy
    (tmp_path / "ArtLapse").mkdir()
    assert data_directory() == tmp_path / "ArtLapse"
    monkeypatch.setenv("SCULPTCUT_DATA_DIR", str(tmp_path / "old-override"))
    assert data_directory() == tmp_path / "old-override"
    monkeypatch.setenv("ARTLAPSE_DATA_DIR", str(tmp_path / "new-override"))
    assert data_directory() == tmp_path / "new-override"
    monkeypatch.setenv("SCULPTCUT_FFMPEG", "old-tool")
    assert environment_value("FFMPEG") == "old-tool"
    monkeypatch.setenv("ARTLAPSE_FFMPEG", "new-tool")
    assert environment_value("FFMPEG") == "new-tool"
    assert (legacy / "settings.ini").read_text(encoding="utf-8") == "preserve"


@pytest.mark.parametrize("executable", ["ArtLapse.exe", "SculptCut.exe"])
def test_both_application_folders_remain_protected_from_project_deletion(
    tmp_path: Path, executable: str,
) -> None:
    root = tmp_path / "application"
    create_empty_project(root)
    original = (root / "project.json").read_bytes()
    (root / executable).write_bytes(b"application marker")
    with pytest.raises(ValueError, match="Эту папку нельзя удалить"):
        project_deletion_target(root)
    load_project(root)
    assert (root / "project.json").read_bytes() == original
