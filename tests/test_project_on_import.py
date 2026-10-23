"""First user import offers a project location and saves a real local project."""

import json
import shutil
from pathlib import Path
from time import monotonic

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from test_webcam import make_video

from sculptcut.project import create_project, load_project
from sculptcut.ui import MainWindow


@pytest.mark.parametrize("method", ["picker", "drop"])
def test_first_import_creates_unique_project_and_preserves_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str,
) -> None:
    source = tmp_path / "скульптинг.mp4"
    second = tmp_path / "продолжение.mp4"
    make_video(source)
    shutil.copy2(source, second)
    original = source.read_bytes()
    parent = tmp_path / "Проекты"
    existing = parent / "скульптинг_ArtLapse"
    existing.mkdir(parents=True)
    marker = existing / "do-not-touch.txt"
    marker.write_text("existing project", encoding="utf-8")
    prompts: list[str] = []

    def choose(*args: object) -> str:
        prompts.append(str(args[1]))
        return str(parent)

    monkeypatch.setattr("sculptcut.ui.QFileDialog.getExistingDirectory", choose)
    monkeypatch.setattr("sculptcut.ui.QFileDialog.getOpenFileNames",
                        lambda *args: ([str(source), str(second)], ""))
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.show()
        window.preset.setCurrentIndex(window.preset.findData("speed"))
        window.output_name.setText("Мой ролик")
        if method == "picker":
            window.add_videos()
        else:
            window.drop_zone.files_dropped.emit([source, second])
        deadline = monotonic() + 15
        while window.job is not None and monotonic() < deadline:
            app.processEvents()
            QTest.qWait(10)
        assert window.job is None
        assert not window.last_error, window.last_error
        root = parent / "скульптинг_ArtLapse-002"
        project = load_project(root)
        assert window.root == root
        assert Path(window.folder.text()) == root
        assert [s.path for s in project.sources] == [source, second]
        assert project.settings.preset == "speed"
        assert project.settings.output_name == "Мой ролик"
        assert len(prompts) == 1 and "новый проект" in prompts[0]
        assert marker.read_text(encoding="utf-8") == "existing project"
        assert source.read_bytes() == original
        assert not list((root / "proxy").iterdir())
        assert not list((root / "renders").iterdir())
    finally:
        if window.job:
            window.job.cancel.set()
            window.job.wait(5000)
            app.processEvents()
        window.close()
        app.processEvents()


@pytest.mark.parametrize("method", ["picker", "drop"])
def test_cancel_location_rolls_back_import_without_touching_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str,
) -> None:
    source = tmp_path / "source.mp4"
    make_video(source)
    original = source.read_bytes()
    monkeypatch.setattr("sculptcut.ui.QFileDialog.getExistingDirectory", lambda *args: "")
    monkeypatch.setattr("sculptcut.ui.QFileDialog.getOpenFileNames",
                        lambda *args: ([str(source)], ""))
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.show()
        if method == "picker":
            window.add_videos()
        else:
            window.drop_zone.files_dropped.emit([source])
        assert window.source_paths() == []
        assert window.folder.text() == ""
        assert window.job is None and window.root is None
        assert not list(tmp_path.glob("*_ArtLapse*"))
        assert window.drop_zone.isVisible()
        assert "отменено" in window.status.text()
        assert source.read_bytes() == original
        assert json.loads(window.recent_settings.value("session_draft"))["sources"] == []
    finally:
        window.close()
        app.processEvents()


def test_existing_project_and_programmatic_restore_do_not_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.mp4"
    second = tmp_path / "second.mp4"
    make_video(source)
    shutil.copy2(source, second)
    root = tmp_path / "existing"
    create_project(root, [source])
    saved = (root / "project.json").read_bytes()

    def unexpected(*args: object) -> str:
        raise AssertionError("An existing destination or draft must not prompt for a new project")

    monkeypatch.setattr("sculptcut.ui.QFileDialog.getExistingDirectory", unexpected)
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.show()
        window.set_sources([source])
        assert window.folder.text() == ""
        window.open_project_folder(root)
        window.add_dropped_videos([second])
        assert window.source_paths() == [source, second]
        assert Path(window.folder.text()) == root
        assert (root / "project.json").read_bytes() == saved
    finally:
        window.close()
        app.processEvents()


def test_cancel_preserves_older_draft_and_explains_missing_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "older.mp4"
    added = tmp_path / "new.mp4"
    make_video(source)
    shutil.copy2(source, added)
    monkeypatch.setattr("sculptcut.ui.QFileDialog.getExistingDirectory", lambda *args: "")
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.show()
        window.set_sources([source])
        window.add_dropped_videos([added])
        assert window.source_paths() == [source]
        assert window.folder.text() == ""
        assert "выберите папку проекта" in window.status.text()
        assert json.loads(window.recent_settings.value("session_draft"))["sources"] == [str(source)]
        assert source.is_file() and added.is_file()
    finally:
        window.close()
        app.processEvents()
