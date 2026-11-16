"""Replace a soundtrack through the actual inline editor and Apply button."""
from threading import Event

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog
from test_cursor_playback import finish, open_editor
from test_episode_panel import wait_until
from test_music_edit import music_movie as music_movie

from sculptcut.music_edit import apply_music_edit, load_draft
from sculptcut.rendering import verify_output
from sculptcut.selection_draft import draft_path


@pytest.mark.parametrize("stale_video_draft", [False, True])
def test_delete_add_apply_reopen_and_export(music_movie, monkeypatch, stale_video_draft):
    root, original, clips = music_movie
    video = apply_music_edit(root, original, clips, Event())
    app = QApplication.instance() or QApplication([])
    window, source = open_editor(root, video, app)
    try:
        music = source.music_track
        assert music is not None
        music.select(0)
        music.lane.setFocus()
        QTest.keyClick(music.lane, Qt.Key.Key_Delete)
        QTest.keyClick(music.lane, Qt.Key.Key_Delete)
        assert not music.clips
        source.seek_montage(0)
        monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *a, **k:(str(clips[1].path),''))
        music.add()
        assert len(music.clips)==1 and music.clips[0].path==clips[1].path
        assert source.apply.isEnabled()
        expected=list(music.clips)
        if stale_video_draft:
            source.session.restore_note = "Видеочерновик относится к другой версии."
            old_draft = draft_path(root,video)
            old_draft.parent.mkdir(parents=True,exist_ok=True)
            old_draft.write_text('{"version":0}',encoding='utf-8')
            preserved = old_draft.read_bytes()
        source.apply.click()
        wait_until(app,lambda:window.job is None,30)
        assert not window.last_error,window.last_error
        wait_until(app,lambda:window.selection_dialog is not None
            and window.selection_dialog is not source and not window.selection_dialog.loading,30)
        source=window.selection_dialog
        assert source.music_track.clips==expected
        assert load_draft(root,source.video)==expected
        if stale_video_draft:
            assert old_draft.read_bytes()==preserved
        verify_output(source.video,90,30,expected_audio=True)
        window.export_button.click()
        wait_until(app,lambda:window.job is None,30)
        assert not window.last_error,window.last_error
    finally:
        finish(window,source,app)


def test_apply_preparation_error_is_visible(music_movie,monkeypatch):
    root,video,clips=music_movie
    app=QApplication.instance() or QApplication([])
    window,source=open_editor(root,video,app)
    try:
        source.music_track.commit(clips)
        def broken_edit():
            raise ValueError('Cannot prepare this edit')
        monkeypatch.setattr(source.session,'export_edit',broken_edit)
        source.apply.click()
        assert 'Cannot prepare this edit' in window.last_error
        assert window.job is None and source.isEnabled()
    finally:
        finish(window,source,app)
