"""Native keys target the hovered lane and time, without a timeline click."""
import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu
from test_cursor_playback import finish, open_editor
from test_episode_panel import wait_until
from test_music_edit import music_movie as music_movie


def hover(app, lane, ms):
    point = QPoint(round(lane.x_at(ms)), 36 if lane.show_ruler else 20)
    app.processEvents()
    app.sendEvent(lane, QMouseEvent(QEvent.Type.MouseMove, QPointF(point),
        QPointF(lane.mapToGlobal(point)), Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
    assert lane.window().selection_dialog.hover_keys.target() is lane
    return lane.time_at(point.x())


@pytest.mark.parametrize('music_lane', [False, True])
def test_hover_split_time_delete_history_and_guard_without_click(music_movie, music_lane):
    root, video, clips = music_movie
    app = QApplication.instance() or QApplication([])
    window, source = open_editor(root, video, app)
    music = source.music_track
    assert music is not None
    try:
        wait_until(app, lambda: music.lane.mapTo(window, QPoint()).y()
                   > source.montage_seek.mapTo(window, QPoint()).y())
        music.commit(clips)
        original = source.session.plan
        lane = music.lane if music_lane else source.montage_seek
        other = source.montage_seek if music_lane else music.lane
        other.setFocus()
        source.seek_montage(2500)
        lane.set_view(500, 1500)
        ms = hover(app, lane, 1000)
        assert app.focusWidget() is other
        original_view = (lane.view_start, lane.view_span)
        app.sendEvent(other, QKeyEvent(QEvent.Type.KeyPress,
            Qt.Key.Key_Q if music_lane else ord('Й'), Qt.KeyboardModifier.NoModifier))
        assert (lane.view_start, lane.view_span) == original_view
        assert abs(lane.value()-ms) <= lane.frame_ms
        if music_lane:
            assert len(music.clips) == 3 and source.session.plan == original
        else:
            assert source.session.changed and music.clips == clips
            assert len(source.montage_seek.blocks) == 2
        # Ctrl+Z goes to the hovered lane even when another lane retains focus.
        window.play_button.setFocus()
        QTest.keyClick(app.focusWidget(), Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert source.session.plan == original and music.clips == clips
        QTest.keyClick(app.focusWidget(), Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
        assert len(music.clips if music_lane else source.montage_seek.blocks) == (
            3 if music_lane else 2)
        QTest.keyClick(app.focusWidget(), Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert music.clips == clips and source.session.plan == original
        # Delete uses the hovered fragment, rather than a stale selected episode.
        lane.reset_view()
        if not music_lane:
            hover(app, lane, 1000)
            QTest.keyClick(app.focusWidget(), Qt.Key.Key_Q)
        before_delete = source.session.plan
        hover(app, lane, 2400)
        if music_lane:
            music.select(0)
        QTest.keyClick(app.focusWidget(), Qt.Key.Key_Delete)
        if music_lane:
            assert music.clips == [clips[0]] and source.session.plan == original
        else:
            assert source.session.plan != before_delete and music.clips == clips
        QTest.keyClick(app.focusWidget(), Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert source.session.plan == before_delete and music.clips == clips
        if not music_lane:
            QTest.keyClick(app.focusWidget(), Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert music.clips == clips and source.session.plan == original
        # Repeated keys and active gestures cannot start an edit on either lane.
        hover(app, lane, 1000)
        app.sendEvent(other, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Q,
            Qt.KeyboardModifier.NoModifier, '', True))
        other.pointer_down = True
        QTest.keyClick(other, Qt.Key.Key_Q)
        other.pointer_down = False
        assert music.clips == clips and source.session.plan == original
        window.output_name.setFocus()
        QTest.keyClick(window.output_name, Qt.Key.Key_Q)
        assert window.output_name.text().endswith('q')
        assert music.clips == clips and source.session.plan == original
        window.play_button.setFocus()
        menu = QMenu(window)
        menu.addAction('Do nothing')
        menu.popup(window.play_button.mapToGlobal(QPoint()))
        app.processEvents()
        assert app.activePopupWidget() is not None
        QTest.keyClick(menu, Qt.Key.Key_Q)
        menu.hide()
        assert music.clips == clips and source.session.plan == original
    finally:
        finish(window, source, app)


def test_hover_music_gap_and_hidden_panel_do_not_edit(music_movie):
    root, video, clips = music_movie
    app = QApplication.instance() or QApplication([])
    window, source = open_editor(root, video, app)
    music = source.music_track
    assert music is not None
    try:
        wait_until(app, lambda: music.lane.mapTo(window, QPoint()).y()
                   > source.montage_seek.mapTo(window, QPoint()).y())
        music.commit(clips)
        original = source.session.plan
        window.play_button.setFocus()
        music.select(0)
        hover(app, music.lane, 1700)
        for key in (Qt.Key.Key_Delete, Qt.Key.Key_Q):
            QTest.keyClick(window.play_button, key)
        assert music.clips == clips and source.session.plan == original
        source.hide()
        QTest.keyClick(window.play_button, Qt.Key.Key_Q)
        assert music.clips == clips and source.session.plan == original
    finally:
        finish(window, source, app)
