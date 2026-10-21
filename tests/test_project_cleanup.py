import os
from dataclasses import replace
from pathlib import Path
from threading import Event
from time import monotonic, sleep

import pytest
from PySide6.QtCore import QSettings, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from sculptcut.cleanup_dialog import CleanupDialog
from sculptcut.export_storage import metadata_path
from sculptcut.media import discover, run_process
from sculptcut.project import create_project, save_project
from sculptcut.project_cleanup import cleanup_files, remove_cleanup_files
from sculptcut.project_deletion import project_deletion_target, remove_project_folder
from sculptcut.proxy import proxy_path
from sculptcut.ui import MainWindow


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    source = tmp_path / "исходник.mp4"
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 "color=c=blue:s=160x90:r=10:d=0.3", "-c:v", "libx264", str(source)])
    root = tmp_path / "проект"
    project = create_project(root, [source])
    for kind in ("renders", "previews"):
        video = root / kind / "готовый.mp4"
        video.write_bytes(source.read_bytes())
        sidecar = metadata_path(video, ".variant.json")
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text('{"schema_version":1}', encoding="utf-8")
    proxy_path(root, project, project.sources[0]).write_bytes(source.read_bytes())
    (root / "renders" / "заметки.txt").write_text("сохранить", encoding="utf-8")
    (root / "proxy" / "чужое.mp4").write_bytes(b"preserve")
    return root


def test_selective_cleanup_preserves_inputs_settings_and_unknown_files(project_root: Path) -> None:
    root = project_root
    before = (root / "project.json").read_bytes()
    source = root.parent / "исходник.mp4"
    original = source.read_bytes()
    files = tuple(f for f in cleanup_files(root) if f.category == "renders")
    result = remove_cleanup_files(root, files, Event(), lambda _: None)
    assert result.removed == 2 and result.bytes_removed == sum(f.size for f in files)
    assert not result.errors and not result.cancelled
    assert not (root / "renders" / "готовый.mp4").exists()
    assert not metadata_path(root / "renders" / "готовый.mp4", ".variant.json").exists()
    assert (root / "previews" / "готовый.mp4").is_file()
    assert (root / "renders" / "заметки.txt").read_text(encoding="utf-8") == "сохранить"
    assert (root / "proxy" / "чужое.mp4").read_bytes() == b"preserve"
    assert source.read_bytes() == original
    assert (root / "project.json").read_bytes() == before


def test_original_inside_managed_folder_and_hardlink_are_protected(project_root: Path) -> None:
    from sculptcut.project import load_project

    root = project_root
    project = load_project(root)
    source = root / "renders" / "готовый.mp4"
    save_project(root, project.model_copy(update={
        "sources": [project.sources[0].model_copy(update={"path": source})],
    }))
    alias = root / "previews" / "alias.mp4"
    os.link(source, alias)
    files = cleanup_files(root)
    assert not any(f.path in (source, alias) for f in files)
    result = remove_cleanup_files(root, files, Event(), lambda _: None)
    assert not result.errors
    assert source.is_file() and alias.is_file()


def test_replaced_files_and_cancelled_deletion_preserve_history(project_root: Path) -> None:
    root = project_root
    files = tuple(f for f in cleanup_files(root) if f.category == "renders")
    cancel = Event()
    cancel.set()
    assert remove_cleanup_files(root, files, cancel, lambda _: None).cancelled
    assert all(f.path.is_file() for f in files)
    files[0].path.write_bytes(b"new file, preserve")
    result = remove_cleanup_files(root, files, Event(), lambda _: None)
    assert result.removed == 0 and len(result.errors) == 2
    assert all(f.path.is_file() for f in files)
    external = root.parent / "исходник.mp4"
    forged = replace(files[0], path=external, video=external)
    assert remove_cleanup_files(root, (forged,), Event(), lambda _: None).errors
    assert external.is_file()


def test_redirected_directory_is_never_followed(project_root: Path) -> None:
    root = project_root
    files = tuple(f for f in cleanup_files(root) if f.category == "renders")
    moved = root / "kept-renders"
    (root / "renders").rename(moved)
    try:
        (root / "renders").symlink_to(moved, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlinks unavailable: {exc}")
    assert not any(f.category == "renders" for f in cleanup_files(root))
    result = remove_cleanup_files(root, files, Event(), lambda _: None)
    assert result.removed == 0 and result.errors
    assert (moved / "готовый.mp4").is_file()


def test_busy_file_keeps_its_metadata_and_partial_cancel_is_reported(
    project_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = project_root
    files = tuple(f for f in cleanup_files(root) if f.category == "renders")
    unlink = Path.unlink

    def locked(path: Path, missing_ok: bool = False) -> None:
        if path == files[0].path:
            raise PermissionError("Файл занят")
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", locked)
    result = remove_cleanup_files(root, files, Event(), lambda _: None)
    assert result.removed == 0 and len(result.errors) == 2
    assert all(f.path.is_file() for f in files)
    monkeypatch.setattr(Path, "unlink", unlink)
    cancel = Event()
    all_files = cleanup_files(root)
    result = remove_cleanup_files(root, all_files, cancel, lambda _: cancel.set())
    assert result.cancelled and result.removed == 2
    assert not any(f.path.exists() for f in files)
    assert (root / "previews" / "готовый.mp4").is_file()


def test_gui_cancel_then_delete_open_output_and_refresh(
    project_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    window.open_project_folder(project_root)
    video = project_root / "renders" / "готовый.mp4"
    window.open_video(video, "Сохранённый вариант")
    QTest.qWait(150)
    try:
        assert window.cleanup_button.isEnabled()
        dialog = CleanupDialog(project_root, cleanup_files(project_root), window)
        assert not dialog.selected_files() and not dialog.remove_button.isEnabled()
        dialog.categories["renders"].setChecked(True)
        assert dialog.remove_button.isEnabled() and dialog.file_list.count() == 2
        dialog.reject()
        assert video.is_file()

        def choose() -> None:
            dialog = next(d for d in window.findChildren(CleanupDialog) if d.isVisible())
            assert not dialog.isWindow()
            dialog.categories["renders"].setChecked(True)
            dialog.remove_button.click()

        QTimer.singleShot(0, lambda: next(
            d for d in window.findChildren(CleanupDialog) if d.isVisible()
        ).reject())
        window.clean_project()
        assert video.is_file() and window.job is None
        QTimer.singleShot(0, choose)
        window.clean_project()
        assert window.job is not None and not window.cleanup_button.isEnabled()
        deadline = monotonic() + 15
        while window.job is not None and monotonic() < deadline:
            app.processEvents()
            sleep(0.01)
        assert window.job is None and not window.last_error
        assert not video.exists() and window.variants.count() == 0
        assert not window.cut_button.isVisible() and window.player.source().isEmpty()
        assert window.cleanup_button.isEnabled()
        assert "Удалено файлов: 2" in window.status.text()
        assert (project_root.parent / "исходник.mp4").is_file()
    finally:
        window.close()
        app.processEvents()


def test_entire_folder_cancel_then_delete_preserves_external_files(project_root: Path) -> None:
    root = project_root
    original = root.parent / "исходник.mp4"
    original_bytes = original.read_bytes()
    neighbour = root.parent / "другой проект"
    neighbour.mkdir()
    (neighbour / "оставить.txt").write_text("оставить", encoding="utf-8")
    target = project_deletion_target(root)
    cancel = Event()
    cancel.set()
    result = remove_project_folder(target, cancel, lambda _: None)
    assert result.cancelled and result.removed == 0
    assert (root / "project.json").is_file()
    expected_size = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
    result = remove_project_folder(target, Event(), lambda _: None)
    assert not result.errors and not result.cancelled
    assert result.bytes_removed == expected_size and not root.exists()
    assert original.read_bytes() == original_bytes
    assert (neighbour / "оставить.txt").read_text(encoding="utf-8") == "оставить"


@pytest.mark.parametrize("asset", ["source", "music_folder", "intro_image", "presentation_video"])
def test_entire_folder_blocks_inputs_inside_project(project_root: Path, asset: str) -> None:
    from sculptcut.project import load_project

    root = project_root
    for invalid in (Path.cwd(), Path.home(), Path(root.anchor)):
        with pytest.raises(ValueError):
            project_deletion_target(invalid)
    project = load_project(root)
    path = root / ("inputs" if asset == "music_folder" else "original.mp4")
    if asset == "music_folder":
        path.mkdir()
    else:
        path.write_bytes(b"protected input")
    if asset == "source":
        project = project.model_copy(update={
            "sources": [project.sources[0].model_copy(update={"path": path})],
        })
    else:
        project = project.model_copy(update={
            "settings": project.settings.model_copy(update={asset: path}),
        })
    save_project(root, project)
    with pytest.raises(ValueError, match="исходники, музыка или заставка"):
        project_deletion_target(root)
    assert path.exists() and (root / "renders" / "готовый.mp4").is_file()


def test_entire_folder_rejects_links_before_removing_any_file(project_root: Path) -> None:
    root = project_root
    target = project_deletion_target(root)
    link = root / "linked-original.mp4"
    original = root.parent / "исходник.mp4"
    os.link(original, link)
    result = remove_project_folder(target, Event(), lambda _: None)
    assert result.errors and result.removed == 0 and original.is_file()
    link.unlink()
    link.symlink_to(original)
    result = remove_project_folder(target, Event(), lambda _: None)
    assert result.errors and result.removed == 0 and original.is_file()
    assert (root / "project.json").is_file()


@pytest.mark.parametrize("mode", ["changed", "busy", "cancel", "new_file", "busy_root"])
def test_entire_folder_failures_keep_project_descriptor(
    project_root: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    root = project_root
    target = project_deletion_target(root)
    cancel = Event()
    if mode == "changed":
        marker = root / "project.json"
        marker.write_text(marker.read_text(encoding="utf-8") + " ", encoding="utf-8")
    if mode == "busy":
        unlink = Path.unlink

        def locked(path: Path, missing_ok: bool = False) -> None:
            if path.name == "готовый.mp4":
                raise PermissionError("Файл занят")
            unlink(path, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", locked)
    if mode == "busy_root":
        rmdir = Path.rmdir

        def locked_root(path: Path) -> None:
            if path == root:
                raise PermissionError("Папка занята")
            rmdir(path)

        monkeypatch.setattr(Path, "rmdir", locked_root)

    def progress(value: float) -> None:
        if value <= 0:
            return
        if mode == "cancel":
            cancel.set()
        elif mode == "new_file":
            (root / "новый.txt").write_text("сохранить", encoding="utf-8")

    result = remove_project_folder(target, cancel, progress)
    assert result.errors or result.cancelled
    assert (root / "project.json").is_file()
    assert (root.parent / "исходник.mp4").is_file()
    if mode == "changed":
        assert result.removed == 0
    if mode == "new_file":
        assert (root / "новый.txt").read_text(encoding="utf-8") == "сохранить"


def test_replaced_project_folder_and_unsaved_input_are_never_deleted(project_root: Path) -> None:
    root = project_root
    target = project_deletion_target(root)
    with pytest.raises(ValueError):
        project_deletion_target(root, [root / "unsaved-original.mp4"])
    moved = root.with_name("original-project")
    assert moved.parent == root.parent and not moved.exists()
    root.rename(moved)
    create_project(root, [root.parent / "исходник.mp4"])
    result = remove_project_folder(target, Event(), lambda _: None)
    assert result.errors and result.removed == 0
    assert (root / "project.json").is_file() and (moved / "project.json").is_file()


def test_full_project_gui_confirmation_then_deletion_resets_session(
    project_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.recent_settings = QSettings(str(project_root.parent / "gui.ini"),
                                       QSettings.Format.IniFormat)
    window.show()
    window.open_project_folder(project_root)
    dialog = CleanupDialog(project_root, cleanup_files(project_root), window)
    dialog.delete_project.setChecked(True)

    def cancel_confirm() -> None:
        confirm = app.activeModalWidget()
        assert isinstance(confirm, QMessageBox)
        assert str(project_root.absolute()) in confirm.informativeText()
        assert confirm.defaultButton().text() == "Отмена"
        confirm.reject()

    QTimer.singleShot(0, cancel_confirm)
    dialog.confirm_selection()
    assert dialog.result() != QDialog.DialogCode.Accepted and project_root.exists()
    dialog.close()

    def approve_confirm() -> None:
        confirm = app.activeModalWidget()
        assert isinstance(confirm, QMessageBox)
        for button in confirm.buttons():
            if confirm.buttonRole(button) == QMessageBox.ButtonRole.DestructiveRole:
                button.click()
                break

    def choose() -> None:
        dialog = next(d for d in window.findChildren(CleanupDialog) if d.isVisible())
        assert not dialog.isWindow()
        dialog.delete_project.setChecked(True)
        QTimer.singleShot(50, approve_confirm)
        dialog.remove_button.click()

    QTimer.singleShot(0, choose)
    try:
        window.clean_project()
        deadline = monotonic() + 15
        while window.job is not None and monotonic() < deadline:
            app.processEvents()
            sleep(0.01)
        assert window.job is None and not window.last_error
        assert not project_root.exists()
        assert not window.folder.text() and window.sources.count() == 0
        assert project_root not in window.recent_project_paths()
        assert window.player.source().isEmpty() and not window.cleanup_button.isEnabled()
        assert '"folder": ""' in str(window.recent_settings.value("session_draft"))
    finally:
        window.close()
        app.processEvents()
    assert not project_root.exists() and (project_root.parent / "исходник.mp4").is_file()
