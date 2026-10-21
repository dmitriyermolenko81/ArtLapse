from hashlib import sha256
from pathlib import Path
from time import monotonic, sleep

import pytest
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog

from sculptcut.media import discover, run_process
from sculptcut.new_project_dialog import NewProjectDialog
from sculptcut.project import create_empty_project, create_project, load_project, new_project_path
from sculptcut.ui import MainWindow


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\\b", "x:y", "NUL", "COM1.txt",
                                 "last.", "bad\x01", "a" * 101])
def test_new_project_name_cannot_escape_or_use_device_names(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError):
        new_project_path(tmp_path, name)
    assert not list(tmp_path.iterdir())


def test_empty_project_is_saved_and_existing_paths_are_never_reused(tmp_path: Path) -> None:
    target = new_project_path(tmp_path, "Модель ёжика")
    created = create_empty_project(target)
    assert load_project(target) == created and created.sources == []
    assert (target / "analysis.sqlite").is_file()
    assert all((target / name).is_dir() for name in
               ("proxy", "thumbnails", "previews", "renders", "temp", "logs"))
    original = (target / "project.json").read_bytes()
    with pytest.raises(ValueError):
        new_project_path(tmp_path, target.name)
    with pytest.raises(FileExistsError):
        create_empty_project(target)
    assert (target / "project.json").read_bytes() == original
    empty = tmp_path / "existing empty folder"
    empty.mkdir()
    with pytest.raises(FileExistsError):
        create_empty_project(empty)
    marker = tmp_path / "file"
    marker.write_text("preserve", encoding="utf-8")
    with pytest.raises(FileExistsError):
        create_empty_project(marker)
    assert marker.read_text(encoding="utf-8") == "preserve"


def wait_job(window: MainWindow, app: QApplication) -> None:
    deadline = monotonic() + 10
    while window.job is not None and monotonic() < deadline:
        app.processEvents()
        sleep(.01)
    assert window.job is None


def panel(window: MainWindow) -> NewProjectDialog:
    return next(d for d in window.findChildren(NewProjectDialog) if d.isVisible())


def test_gui_create_cancel_recent_reopen_and_import_real_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    clip = tmp_path / "source.mp4"
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 "testsrc2=s=64x64:r=10:d=1", "-c:v", "libx264", str(clip)])
    original_clip = sha256(clip.read_bytes()).hexdigest()
    old = tmp_path / "previous"
    create_project(old, [clip])
    original_project = (old / "project.json").read_bytes()
    window = MainWindow()
    window.show()
    window.open_project_folder(old)
    window.output_name.setText("Несохранённое имя")
    window.music.setText(str(tmp_path / "music"))
    before = window.form_draft()
    target = tmp_path / "Новый проект с кнопки"

    def configure(accept: bool) -> None:
        dialog = panel(window)
        assert not dialog.isWindow()
        dialog.directory.setText(str(tmp_path))
        dialog.name.setText(target.name)
        dialog.create_button.click() if accept else dialog.reject()

    try:
        for size in ((1440, 860), (1120, 700)):
            window.resize(*size)
            app.processEvents()
            assert window.size().width() == size[0]
            for control in (window.new_project_button, window.folder, window.language_button):
                assert window.rect().contains(control.mapTo(window, QPoint()))
                assert window.rect().contains(control.mapTo(window, control.rect().bottomRight()))
        QTimer.singleShot(30, lambda: configure(False))
        window.new_project_button.click()
        assert window.form_draft() == before and not target.exists()

        # Native folder selection is cancel-safe and returns to the same panel.
        dialog = NewProjectDialog(tmp_path, window)
        monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a: "")
        dialog.choose_directory()
        assert not dialog.property("nativeDialogOpen") and dialog.directory.text() == str(tmp_path)
        dialog.close()

        window.activateWindow()
        app.processEvents()
        # Button shortcuts use Qt's short animateClick delay before clicked is emitted.
        QTimer.singleShot(150, lambda: configure(True))
        QTest.keyClick(window.windowHandle(), Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier)
        QTest.qWait(220)
        wait_job(window, app)
        assert not window.last_error
        assert load_project(target).sources == []
        assert window.root == target and window.source_paths() == []
        assert not window.music.text() and not window.output_name.text()
        assert window.presentation_video is None and not window.variants.count()
        assert window.recent_project_paths()[0] == target
        assert (old / "project.json").read_bytes() == original_project
        assert not list((target / "renders").iterdir())
        window.set_sources([clip])
        window.save_current_project()
        wait_job(window, app)
        assert not window.last_error and len(load_project(target).sources) == 1
        window.open_project_folder(target)
        assert window.source_paths() == [clip.resolve()]
        assert sha256(clip.read_bytes()).hexdigest() == original_clip
    finally:
        window.close()
        app.processEvents()


def test_creation_error_and_conflict_leave_current_form_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    window.folder.setText(str(tmp_path / "current"))
    window.output_name.setText("Сохранить форму")
    before = window.form_draft()
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    marker = occupied / "keep.txt"
    marker.write_text("preserve", encoding="utf-8")

    def conflict() -> None:
        dialog = panel(window)
        dialog.directory.setText(str(tmp_path))
        dialog.name.setText(occupied.name)
        assert not dialog.create_button.isEnabled() and dialog.error.isVisible()
        dialog.create_button.click()
        assert dialog.isVisible()
        dialog.reject()

    def fail_creation(root: Path) -> None:
        raise PermissionError("Creation denied for test")

    def choose() -> None:
        dialog = panel(window)
        dialog.directory.setText(str(tmp_path))
        dialog.name.setText("cannot-create")
        dialog.create_button.click()

    try:
        QTimer.singleShot(30, conflict)
        window.new_project_button.click()
        assert window.form_draft() == before and marker.read_text(encoding="utf-8") == "preserve"
        monkeypatch.setattr("sculptcut.project.create_empty_project", fail_creation)
        QTimer.singleShot(30, choose)
        window.new_project_button.click()
        wait_job(window, app)
        assert window.last_error and window.form_draft() == before
        assert not (tmp_path / "cannot-create").exists()
    finally:
        window.close()
        app.processEvents()
