"""Deleting an open preview must release the inline editor's media handles."""
import shutil

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox
from test_episode_panel import wait_until
from test_episode_roll import roll_movie as roll_movie

from sculptcut.cleanup_dialog import CleanupDialog
from sculptcut.export_storage import metadata_path
from sculptcut.ui import MainWindow


@pytest.mark.parametrize(('whole', 'hidden'), [(True, False), (True, True), (False, True)])
def test_cleanup_closes_playing_inline_preview(roll_movie, monkeypatch, whole, hidden):
    import sculptcut.project_cleanup as cleanup
    import sculptcut.project_deletion as deletion

    root, project, _, _, rendered = roll_movie
    video = root/'previews'/rendered.name
    shutil.copy2(rendered, video)
    metadata_path(video, '.variant.json').parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(metadata_path(rendered, '.variant.json'), metadata_path(video, '.variant.json'))
    assets = rendered.parent/'.variant-assets'
    if assets.exists():
        shutil.copytree(assets, video.parent/'.variant-assets', dirs_exist_ok=True)
    originals = {s.path: s.path.read_bytes() for s in project.sources}
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    window.folder.setText(str(root))
    window.open_video(video, 'Preview')
    editor = None
    try:
        wait_until(app, lambda: window.selection_dialog is not None
                   and not window.selection_dialog.loading, 30)
        editor = window.selection_dialog
        editor.play_montage()
        wait_until(app, editor.playback.is_advancing, 20)
        if hidden:
            editor.hide()
        # Cancel leaves the live editor and its selection intact.
        QTimer.singleShot(0, lambda: next(
            d for d in window.findChildren(CleanupDialog) if d.isVisible()).reject())
        window.clean_project()
        assert window.selection_dialog is editor and not editor.closed and video.exists()

        module = deletion if whole else cleanup
        name = 'remove_project_folder' if whole else 'remove_cleanup_files'
        remove = getattr(module, name)
        reached = []

        def released_then_remove(*args, **kwargs):
            reached.append(True)
            assert editor.closed and window.selection_dialog is None
            assert editor.playback.player.source().isEmpty()
            assert editor.music_track.player.source().isEmpty()
            return remove(*args, **kwargs)

        monkeypatch.setattr(module, name, released_then_remove)

        def approve():
            box = app.activeModalWidget()
            assert isinstance(box, QMessageBox)
            next(b for b in box.buttons() if box.buttonRole(b)
                 == QMessageBox.ButtonRole.DestructiveRole).click()

        def choose():
            dialog = next(d for d in window.findChildren(CleanupDialog) if d.isVisible())
            if whole:
                dialog.delete_project.setChecked(True)
                QTimer.singleShot(0, approve)
            else:
                dialog.categories['previews'].setChecked(True)
            dialog.remove_button.click()

        QTimer.singleShot(0, choose)
        window.clean_project()
        wait_until(app, lambda: window.job is None, 30)
        assert reached and not window.last_error
        assert not video.exists()
        assert root.exists() != whole
        assert all(p.read_bytes() == data for p, data in originals.items())
    finally:
        if window.selection_dialog is not None:
            window.selection_dialog.reject()
        window.close()
        app.processEvents()
