import os
from pathlib import Path
from threading import Event

import pytest
from PySide6.QtCore import QEventLoop, QPoint, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox
from test_episode_panel import wait_until
from test_project_cleanup import project_root as project_root

from sculptcut.export_storage import metadata_path
from sculptcut.i18n import tr
from sculptcut.project import load_project, save_project
from sculptcut.ui import MainWindow
from sculptcut.variant_deletion import delete_variant, variant_files
from sculptcut.variants import read_variant, save_variant


def test_delete_one_video_and_sidecars_keeps_neighbours_and_inputs(project_root: Path) -> None:
    root = project_root
    video = root / "renders/готовый.mp4"
    guard = metadata_path(video, ".guard.json")
    guard.write_text("{}", encoding="utf-8")
    preserve = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()
                and p not in (video, guard, metadata_path(video, ".variant.json"))}
    files = variant_files(root, video)
    result = delete_variant(root, video, files, Event(), lambda _: None)
    assert result.removed == 3 and not result.errors
    assert not video.exists() and not guard.exists()
    assert not metadata_path(video, ".variant.json").exists()
    assert all(p.read_bytes() == data for p, data in preserve.items())


def test_cut_child_blocks_deletion_even_when_created_after_confirmation(project_root: Path) -> None:
    root = project_root
    parent = root / "renders/готовый.mp4"
    files = variant_files(root, parent)
    child = parent.with_name("cut.mp4")
    child.write_bytes(parent.read_bytes())
    save_variant(child, parent=parent, operation="cut", cuts=[(0, 100_000)])
    with pytest.raises(ValueError, match="cut.mp4"):
        delete_variant(root, parent, files, Event(), lambda _: None)
    assert parent.is_file() and metadata_path(parent, ".variant.json").is_file()
    assert child.is_file()


def test_self_contained_child_keeps_shared_music_when_parent_deleted(project_root: Path) -> None:
    root = project_root
    parent = root / "renders/music-parent.mp4"
    parent.write_bytes((root / "renders/готовый.mp4").read_bytes())
    music = root / "track.wav"
    music.write_bytes(b"saved soundtrack")
    save_variant(parent, music=music)
    child = parent.with_name("music-child.mp4")
    child.write_bytes(parent.read_bytes())
    save_variant(child, parent=parent, operation="music")
    soundtrack = (child.parent / read_variant(child)["music"]["path"]).resolve()
    result = delete_variant(root, parent, variant_files(root, parent), Event(), lambda _: None)
    assert not result.errors and not parent.exists() and child.is_file()
    assert soundtrack.read_bytes() == b"saved soundtrack"
    assert read_variant(child)["music"]


@pytest.mark.parametrize("kind", ["source", "hardlink", "unsaved_input", "outside"])
def test_delete_refuses_protected_inputs_and_external_paths(project_root: Path, kind: str) -> None:
    root = project_root
    video = root / "renders/готовый.mp4"
    project = load_project(root)
    extra = []
    if kind in ("source", "hardlink"):
        save_project(root, project.model_copy(update={
            "sources": [project.sources[0].model_copy(update={"path": video})],
        }))
        if kind == "hardlink":
            alias = video.with_name("alias.mp4")
            os.link(video, alias)
            video = alias
    elif kind == "unsaved_input":
        extra = [video]
    else:
        video = project.sources[0].path
    before = video.read_bytes()
    with pytest.raises(ValueError):
        variant_files(root, video, extra)
    assert video.read_bytes() == before


def test_changed_cancelled_or_locked_video_keeps_history(
    project_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = project_root
    video = root / "renders/готовый.mp4"
    files = variant_files(root, video)
    cancel = Event()
    cancel.set()
    assert delete_variant(root, video, files, cancel, lambda _: None).cancelled
    video.write_bytes(video.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="изменился"):
        delete_variant(root, video, files, Event(), lambda _: None)
    files = variant_files(root, video)
    unlink = Path.unlink

    def locked(path: Path, missing_ok: bool = False) -> None:
        if path == video:
            raise PermissionError("busy")
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", locked)
    result = delete_variant(root, video, files, Event(), lambda _: None)
    assert result.removed == 0 and result.errors
    assert metadata_path(video, ".variant.json").is_file()


def wait_job(window: MainWindow) -> None:
    if window.job is not None:
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        window.job.finished.connect(loop.quit)
        timer.start(10_000)
        loop.exec()
        timer.stop()
    assert window.job is None and window.job_succeeded, window.last_error


@pytest.mark.parametrize("language", ["ru", "en"])
def test_real_trash_button_cancel_then_delete_open_versions(
    project_root: Path, language: str,
) -> None:
    app = QApplication.instance() or QApplication([])
    root = project_root
    first = root / "renders/готовый.mp4"
    second = first.with_name("other.mp4")
    second.write_bytes(first.read_bytes())
    window = MainWindow()
    try:
        window.change_language(language)
        window.show()
        window.open_project_folder(root)
        index = window.variants.findData(str(first))
        window.variants.setCurrentIndex(index)
        window.open_variant(index)
        assert window.delete_variant_button.isEnabled()
        assert not window.delete_variant_button.icon().isNull()
        assert window.delete_variant_button.toolTip() == tr("Удалить выбранную версию")
        confirmations = []

        def answer(remove: bool) -> None:
            dialog = window.findChild(QMessageBox, "deleteVariantConfirmation")
            assert dialog is not None
            confirmations.append(dialog.text())
            assert first.name in dialog.text() or second.name in dialog.text()
            assert dialog.defaultButton() == dialog.button(QMessageBox.StandardButton.Cancel)
            button = (next(button for button in dialog.buttons()
                           if dialog.buttonRole(button) == QMessageBox.ButtonRole.DestructiveRole)
                      if remove else dialog.button(QMessageBox.StandardButton.Cancel))
            button.click()

        QTimer.singleShot(0, lambda: answer(False))
        window.delete_variant_button.click()
        assert first.exists() and window.job is None
        assert Path(window.player.source().toLocalFile()) == first
        for video in (first, second):
            QTimer.singleShot(0, lambda: answer(True))
            window.delete_variant_button.click()
            assert window.job is not None and not window.delete_variant_button.isEnabled()
            wait_job(window)
            assert not video.exists()
        assert len(confirmations) == 3
        assert window.variants.count() == 0 and window.player.source().isEmpty()
        assert not window.delete_variant_button.isEnabled()
        assert not window.versions_panel.isVisible()
        assert (root.parent / "исходник.mp4").is_file()
        assert (root / "previews/готовый.mp4").is_file()
    finally:
        window.change_language("ru")
        if window.job is not None:
            window.job.cancel.set()
            window.job.wait(10_000)
        window.close()
        app.processEvents()


def test_trash_targets_combo_selection_and_preserves_other_playback(project_root: Path) -> None:
    app = QApplication.instance() or QApplication([])
    root = project_root
    playing = root / "renders/готовый.mp4"
    selected = playing.with_name("selected.mp4")
    selected.write_bytes(playing.read_bytes())
    window = MainWindow()
    try:
        window.resize(1120, 700)
        window.show()
        window.open_project_folder(root)
        window.open_variant(window.variants.findData(str(playing)))
        window.variants.setCurrentIndex(window.variants.findData(str(selected)))

        def accept() -> None:
            dialog = window.findChild(QMessageBox, "deleteVariantConfirmation")
            assert dialog is not None and selected.name in dialog.text()
            next(button for button in dialog.buttons()
                 if dialog.buttonRole(button) == QMessageBox.ButtonRole.DestructiveRole).click()

        QTimer.singleShot(0, accept)
        window.delete_variant_button.click()
        wait_job(window)
        assert not selected.exists() and playing.exists()
        assert Path(window.player.source().toLocalFile()) == playing
        assert window.last_result_video == playing
        assert window.versions_panel.rect().contains(window.delete_variant_button.geometry())
        child = playing.with_name("dependent.mp4")
        child.write_bytes(playing.read_bytes())
        save_variant(child, parent=playing, operation="cut", cuts=[(0, 100_000)])
        window.refresh_variants()
        window.variants.setCurrentIndex(window.variants.findData(str(playing)))
        window.delete_variant_button.click()
        assert window.job is None and playing.exists()
        assert child.name in window.last_error
    finally:
        window.close()
        app.processEvents()


@pytest.mark.parametrize("size", [(1120, 700), (1440, 860)])
@pytest.mark.parametrize("last", [False, True])
def test_deleting_keeps_geometry_during_worker_and_neighbour_reload(
    project_root: Path, monkeypatch: pytest.MonkeyPatch, size: tuple[int, int], last: bool,
) -> None:
    import sculptcut.variant_deletion as deletion

    app = QApplication.instance() or QApplication([])
    root = project_root
    selected = root / "renders/готовый.mp4"
    if not last:
        selected.with_name("remaining.mp4").write_bytes(selected.read_bytes())
    entered, release = Event(), Event()
    original = deletion.delete_variant

    def held(*args, **kwargs):
        entered.set()
        args[4](0.5)  # Exercise progress signals while the worker remains busy.
        if not release.wait(10):
            raise RuntimeError("Deletion geometry check timed out")
        return original(*args, **kwargs)

    monkeypatch.setattr(deletion, "delete_variant", held)
    window = MainWindow()
    try:
        window.resize(*size)
        window.show()
        window.open_project_folder(root)
        index = window.variants.findData(str(selected))
        window.variants.setCurrentIndex(index)
        window.open_variant(index)
        wait_until(app, lambda: not window.video_frame_image.isNull())
        window.player.pause()
        QTest.qWait(100)

        def geometry():
            return [(control.mapTo(window, QPoint()), control.size()) for control in (
                window.main_splitter, window.video, window.seek, window.versions_panel,
                window.settings_scroll, window.duration_card, window.create_button,
            )]

        before = geometry()

        def accept() -> None:
            dialog = window.findChild(QMessageBox, "deleteVariantConfirmation")
            assert dialog is not None
            next(button for button in dialog.buttons()
                 if dialog.buttonRole(button) == QMessageBox.ButtonRole.DestructiveRole).click()

        QTimer.singleShot(0, accept)
        window.delete_variant_button.click()
        wait_until(app, entered.is_set)
        QTest.qWait(100)
        assert geometry() == before
        assert not window.progress_bar.isVisible()
        assert not window.progress_details.isVisible()
        assert not window.cancel_button.isVisible()
        assert not window.delete_variant_button.isEnabled()
        release.set()
        wait_job(window)
        assert not selected.exists()
        if not last:
            wait_until(app, lambda: not window.video_frame_image.isNull())
            QTest.qWait(100)
            assert geometry() == before
        else:
            assert window.variants.count() == 0 and window.player.source().isEmpty()
    finally:
        release.set()
        if window.job is not None:
            wait_job(window)
        window.close()
        app.processEvents()
