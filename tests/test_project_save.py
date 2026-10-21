import hashlib
import time
from pathlib import Path
from threading import Event

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from sculptcut.domain import Roi
from sculptcut.media import discover, run_process
from sculptcut.project import load_project
from sculptcut.ui import MainWindow


@pytest.mark.parametrize("size", [(1120, 700), (1440, 860)])
def test_saving_preserves_panel_geometry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: tuple[int, int],
) -> None:
    import sculptcut.ui as ui

    app = QApplication.instance() or QApplication([])
    source = tmp_path / "input.mp4"
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 "color=s=64x64:d=1", "-c:v", "libx264", str(source)])
    entered, release = Event(), Event()
    original_save = ui.save_project

    def held_save(root, project):
        entered.set()
        if not release.wait(10):
            raise RuntimeError("Timed out waiting for save geometry check")
        original_save(root, project)

    monkeypatch.setattr(ui, "save_project", held_save)
    window = MainWindow()
    window.resize(*size)
    window.show()
    root = tmp_path / "project"

    def geometry():
        return (window.main_splitter.geometry(),
                window.area_stack.mapTo(window, QPoint(0, 0)), window.area_stack.size())

    try:
        window.folder.setText(str(root))
        window.set_sources([source])
        QTest.qWait(250)
        before = geometry()
        window.save_button.click()
        deadline = time.monotonic() + 10
        while not entered.is_set() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert entered.is_set()
        QTest.qWait(100)
        assert geometry() == before
        assert not window.progress_details.isVisible()
        assert not window.progress_bar.isVisible()
        assert not window.cancel_button.isVisible()
        release.set()
        wait_job(window, app)
        QTest.qWait(100)
        assert geometry() == before
        assert load_project(root).settings.target_seconds == 600
        assert not list((root / "renders").glob("*"))
    finally:
        release.set()
        wait_job(window, app)
        window.close()
        app.processEvents()


@pytest.mark.parametrize("preset", ["beat_shorts", "photoshop_beat_shorts"])
def test_music_mode_restores_automatic_sync_from_legacy_project(
    tmp_path: Path, preset: str,
) -> None:
    from sculptcut.domain import ProjectSettings
    from sculptcut.project import create_project, save_project

    app = QApplication.instance() or QApplication([])
    source = tmp_path / "input.mp4"
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 "color=s=64x64:d=1", "-c:v", "libx264", str(source)])
    root = tmp_path / "project"
    project = create_project(root, [source])
    project = project.model_copy(update={"settings": ProjectSettings.model_validate({
        "preset": preset, "beat_sync_enabled": False, "beat_sync_strength": 0,
    })})
    save_project(root, project)
    window = MainWindow()
    try:
        window.open_project_folder(root)
        assert window.preset.currentData() == preset
        assert not hasattr(window, "beat_sync")
        assert "beat_sync" not in window.form_draft()
        window.save_button.click()
        wait_job(window, app)
        settings = load_project(root).settings
        assert settings.beat_sync_enabled
        assert settings.beat_sync_strength == 1
        assert settings.beat_sync_mode == "fast"
    finally:
        window.close()
        app.processEvents()


def wait_job(window: MainWindow, app: QApplication) -> None:
    deadline = time.monotonic() + 20
    while window.job and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert window.job is None
    assert not window.last_error


@pytest.mark.parametrize("name", ["", "Тест"])
def test_save_without_export_and_restore(tmp_path: Path, name: str) -> None:
    app = QApplication.instance() or QApplication([])
    source = tmp_path / "input.mp4"
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 "color=s=64x64:d=1", "-c:v", "libx264", str(source)])
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    window = MainWindow()
    window.show()
    root = tmp_path / "project"
    try:
        window.folder.setText(str(root))
        window.set_sources([source])
        window.target.setValue(2)
        window.output_name.setText(name)
        window.save_button.click()
        wait_job(window, app)
        project = load_project(root)
        assert project.settings.target_seconds == 120
        assert project.settings.output_name == name
        assert not list((root / "renders").glob("*"))
        assert not list((root / "proxy").glob("*"))
        project.rois[project.sources[0].id] = Roi(x=0.1, y=0.1, width=0.5, height=0.5)
        from sculptcut.project import save_project

        save_project(root, project)
        window.output_name.setText("Новое имя")
        window.save_button.click()
        wait_job(window, app)
        assert load_project(root).rois == project.rois
        reopened = MainWindow()
        reopened.open_project_folder(root)
        assert reopened.output_name.text() == "Новое имя"
        reopened.close()
        assert hashlib.sha256(source.read_bytes()).hexdigest() == original
    finally:
        window.close()
        app.processEvents()


def test_draft_recovers_incomplete_form(monkeypatch: pytest.MonkeyPatch) -> None:
    app = QApplication.instance() or QApplication([])
    first = MainWindow()
    first.output_name.setText("Черновик")
    first.preset.setCurrentIndex(first.preset.findData("photoshop_beat_shorts"))
    first.target.setValue(0)
    first.normal_minutes = 23
    first.square_rois[Path("missing.mp4").resolve()] = Roi(x=0, y=0, width=0.5, height=0.5)
    first.autosave_draft()
    second = MainWindow()
    monkeypatch.setattr(second, "confirm_action", lambda *_: True)
    try:
        second.recover_session()
        assert second.output_name.text() == "Черновик"
        assert second.target.value() == 0
        assert second.preset.currentData() == "photoshop_beat_shorts"
        assert not hasattr(second, "beat_sync")
        assert second.square_rois == first.square_rois
        assert second.normal_minutes == 23
        assert second.job is None
    finally:
        first.close()
        second.close()
        app.processEvents()


def test_restore_settings_without_sources_or_name(monkeypatch: pytest.MonkeyPatch) -> None:
    app = QApplication.instance() or QApplication([])
    first = MainWindow()
    first.target.setValue(18)
    first.autosave_draft()
    second = MainWindow()
    monkeypatch.setattr(second, "confirm_action", lambda *_: True)
    try:
        second.recover_session()
        assert second.target.value() == 18
        assert not second.sources.count()
        assert not second.output_name.text()
    finally:
        first.close()
        second.close()
        app.processEvents()


def test_corrupt_draft_and_decline(monkeypatch: pytest.MonkeyPatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window._recover_after_crash = True
    try:
        window.recent_settings.setValue("session_draft", "[]")
        window.recover_session()
        assert "восстановить" in window.last_error
        window.output_name.setText("draft")
        window.autosave_draft()
        window.output_name.clear()
        monkeypatch.setattr(window, "confirm_action", lambda *_: False)
        window.recover_session()
        assert not window.output_name.text()
        assert not window.recent_settings.contains("session_draft")
    finally:
        window.close()
        app.processEvents()


def test_clean_close_skips_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    app = QApplication.instance() or QApplication([])
    first = MainWindow()
    first.output_name.setText("Сохранённый сеанс")
    first.autosave_draft()
    first.close()
    second = MainWindow()
    monkeypatch.setattr(second, "confirm_action", lambda *_: pytest.fail("Unexpected recovery"))
    try:
        second.recover_session()
        assert not second.output_name.text()
        assert second.recent_settings.value("session_running", False, type=bool)
    finally:
        second.close()
        app.processEvents()
