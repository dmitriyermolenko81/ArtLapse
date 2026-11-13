"""Inline music-style review protects edits while keeping frame navigation cheap."""
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QFocusEvent, QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from test_cursor_playback import finish
from test_episode_panel import wait_until
from test_episode_roll import roll_movie as roll_movie

from sculptcut.review_timeline import ReviewTimeline
from sculptcut.timeline_appearance import LanePiece
from sculptcut.ui import MainWindow


@pytest.mark.parametrize('compact', [False, True])
def test_review_click_jitter_double_click_focus_loss_and_repeat_are_safe(compact):
    app = QApplication.instance() or QApplication([])
    track = ReviewTimeline()
    track.resize(900, 104)
    if compact:
        track.compact_lane = True
        track.left_padding = 56
        track.marker_y = 56
        track.setFixedHeight(62)
    track.setRange(0, 6000)
    track.blocks = [(0, 0, 2000), (1, 2000, 6000)]
    track.pieces = [LanePiece(0, 0, 2000, '1 · sculpture', '00:00 – 00:06'),
                    LanePiece(1, 2000, 6000, '2 · sculpture', '00:06 – 00:12')]
    edits, previews, keys = [], [], []
    track.edge_commit.connect(lambda *args: edits.append(args))
    track.movie_commit.connect(lambda *args: edits.append(args))
    track.edge_preview.connect(lambda *args: previews.append(args))
    for signal in (track.split_scene, track.remove_scene,
                   track.undo_requested, track.redo_requested):
        signal.connect(lambda: keys.append(True))
    track.show()
    point = QPoint(round(track.x_at(2000)), 48 if compact else 50)
    try:
        QTest.mouseClick(track, Qt.MouseButton.LeftButton, pos=point)
        QTest.mousePress(track, Qt.MouseButton.LeftButton, pos=point)
        QTest.mouseMove(track, point+QPoint(2, 0))
        QTest.mouseRelease(track, Qt.MouseButton.LeftButton, pos=point+QPoint(2, 0))
        QTest.mouseDClick(track, Qt.MouseButton.LeftButton, pos=point)
        assert not edits
        for cancel in ('escape', 'focus', 'hide'):
            QTest.mousePress(track, Qt.MouseButton.LeftButton, pos=point)
            QTest.mouseMove(track, point+QPoint(-45, 0))
            assert previews and not edits
            if cancel == 'escape':
                QTest.keyClick(track, Qt.Key.Key_Escape)
            elif cancel == 'focus':
                app.sendEvent(track, QFocusEvent(QEvent.Type.FocusOut))
            else:
                track.hide()
                track.show()
            QTest.mouseRelease(track, Qt.MouseButton.LeftButton, pos=point+QPoint(-45, 0))
            assert not edits and not track.pointer_down
        for key, mods in ((Qt.Key.Key_Q, Qt.KeyboardModifier.NoModifier),
                          (Qt.Key.Key_Delete, Qt.KeyboardModifier.NoModifier),
                          (Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier),
                          (Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)):
            app.sendEvent(track, QKeyEvent(QEvent.Type.KeyPress, key, mods, '', True))
        QTest.keyClick(track, Qt.Key.Key_Backspace, Qt.KeyboardModifier.ControlModifier)
        assert not keys
        QTest.mousePress(track, Qt.MouseButton.LeftButton, pos=point)
        QTest.mouseMove(track, point+QPoint(-45, 0))
        assert not edits
        QTest.mouseRelease(track, Qt.MouseButton.LeftButton, pos=point+QPoint(-45, 0))
        assert len(edits) == 1 and edits[0][0:2] == (track.selected_row, 'end')
    finally:
        track.close()
        app.processEvents()


def test_main_inline_review_reuses_scene_state_and_preserves_typed_edits(roll_movie, monkeypatch):
    root, _, _, _, video = roll_movie
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    dialog = None
    window.show()
    window.folder.setText(str(root))
    window.open_video(video, 'test')
    try:
        wait_until(app, lambda: window.selection_dialog is not None
                   and not window.selection_dialog.loading)
        dialog = window.selection_dialog
        wait_until(app, lambda: not dialog.frame_image.isNull())
        track = dialog.montage_seek
        assert isinstance(track, ReviewTimeline) and not dialog.isWindow()
        assert track.window() is window and track.height() == 62
        original = dialog.session.plan
        _, start, end = track.blocks[0]
        dialog.seek_montage(start+100)
        choose = dialog.choose_scene
        calls = []
        def counted(*args, **kwargs):
            calls.append(True)
            return choose(*args, **kwargs)
        monkeypatch.setattr(dialog, 'choose_scene', counted)
        for i in range(10):
            dialog.seek_montage(min(end-1, start+100+i*10), follow=False)
        assert not calls and dialog.saved_scrub
        assert dialog.scrub_frames.wanted[0] == video.resolve()
        dialog.more.click()
        assert dialog.fragment_menu.isVisible()
        dialog.fragment_menu.actions()[0].trigger()
        dialog.fragment_menu.hide()
        assert dialog.details.isVisible()
        dialog.start.setFocus()
        QTest.keyClick(dialog.start, Qt.Key.Key_Return)
        QTest.keyClick(dialog.start, Qt.Key.Key_Q)
        global_undos = []
        window.edit_history.record('Guard input', 0, 1, global_undos.append)
        history_position = window.edit_history.position
        QTest.keyClick(dialog.start, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(dialog.start, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
        assert window.edit_history.position == history_position and not global_undos
        assert window.job is None and dialog.session.plan == original
        dialog.details.hide()
        track.setFocus()
        track.navigate((start+end)//2)
        dialog.flush_navigation()
        QTest.keyClick(track, Qt.Key.Key_Q)
        assert dialog.session.plan != original
        QTest.keyClick(track, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert dialog.session.plan == original
        window.resize(1120, 760)
        app.processEvents()
        folder = Path('artifacts/compact-tracks-327')
        folder.mkdir(exist_ok=True, parents=True)
        assert window.grab().save(str(folder/'main-inline.png'))
        assert track.grab().save(str(folder/'review-lane.png'))
    finally:
        if dialog is not None:
            finish(window, dialog, app)
        else:
            window._allow_close = True
            window.close()
