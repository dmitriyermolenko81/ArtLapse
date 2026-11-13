"""Exercise real timeline pointer events rather than direct seek method calls."""
import pytest
from PySide6.QtCore import QEvent, QEventLoop, QPointF, Qt, QTimer
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication
from test_cursor_playback import finish, open_editor
from test_episode_panel import wait_until
from test_episode_roll import roll_movie as roll_movie

from sculptcut.source_selection_dialog import SourceSelectionDialog
from sculptcut.time_format import format_time


@pytest.mark.parametrize('edited', [False, True])
def test_playing_seek_resumes_but_paused_seek_stays_paused(roll_movie, edited):
    app = QApplication.instance() or QApplication([])
    root, _, _, _, video = roll_movie
    window, dialog = open_editor(root, video, app)
    track = dialog.montage_seek

    def pointer(kind, ms):
        point = QPointF(track.x_at(ms), 43)
        release = kind == QEvent.Type.MouseButtonRelease
        app.sendEvent(track, QMouseEvent(kind, point, point,
            Qt.MouseButton.LeftButton if kind != QEvent.Type.MouseMove
            else Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton if release else Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier))

    def click(ms):
        pointer(QEvent.Type.MouseButtonPress, ms)
        pointer(QEvent.Type.MouseButtonRelease, ms)

    try:
        if edited:
            row, a, b = track.blocks[0]
            dialog.commit_edge(row, 'end', (a+b)//2)
        dialog.seek_montage(track.blocks[0][1]+50)
        window.play_button.click()
        wait_until(app, dialog.playback.is_advancing, 20)
        plan = dialog.session.plan
        backend = dialog.playback.player
        for target in (track.blocks[1][1]+200, track.blocks[0][1]+100):
            click(target)
            assert dialog.sequence.desired
            wait_until(app, lambda target=target: dialog.playback.is_advancing()
                       and dialog.sequence.cursor > target+150, 20)
            assert dialog.sequence.cursor < target+1400
            if not edited:
                assert dialog.sequence.direct and dialog.playback.player is backend
                assert dialog.sequence.preparer.worker is None
        # A held gesture previews frames without restarting a context per move.
        pointer(QEvent.Type.MouseButtonPress, track.blocks[1][1]+50)
        dialog.navigate()
        assert dialog.resume_navigation and not dialog.sequence.desired
        target = track.blocks[1][1]+250
        pointer(QEvent.Type.MouseMove, target)
        dialog.navigate()
        assert dialog.resume_navigation and not dialog.sequence.desired
        pointer(QEvent.Type.MouseButtonRelease, target)
        wait_until(app, lambda: dialog.playback.is_advancing()
                   and dialog.sequence.cursor > target+100, 20)
        assert dialog.session.plan == plan
        window.play_button.click()
        assert not dialog.sequence.desired
        click(track.blocks[0][1]+100)
        assert not dialog.sequence.desired and not dialog.playback.want_play
        assert not dialog.resume_navigation
        # Keyboard navigation uses the same completion without a mouse release.
        window.play_button.click()
        wait_until(app, dialog.playback.is_advancing, 20)
        target = track.blocks[1][1]+100
        dialog.request_montage_seek(target)
        wait_until(app, lambda: dialog.playback.is_advancing()
                   and dialog.sequence.cursor > target+100, 20)
        assert dialog.sequence.desired
    finally:
        if track.pointer_down:
            pointer(QEvent.Type.MouseButtonRelease, track.value())
        finish(window, dialog, app)


def test_pointer_burst_coalesces_and_live_drag_shows_frames_before_release(roll_movie, monkeypatch):
    app = QApplication.instance() or QApplication([])
    calls = []
    original = SourceSelectionDialog.seek_montage
    def counted(self, ms, **kwargs):
        calls.append(ms)
        return original(self, ms, **kwargs)
    monkeypatch.setattr(SourceSelectionDialog, 'seek_montage', counted)
    root, _, _, _, video = roll_movie
    window, dialog = open_editor(root, video, app)
    track = dialog.montage_seek
    timer = QTimer()
    def pointer(kind, x, button=Qt.MouseButton.NoButton):
        point = QPointF(x, 43)
        app.sendEvent(track, QMouseEvent(kind, point, point, button,
            Qt.MouseButton.LeftButton if kind != QEvent.Type.MouseButtonRelease
            else Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
    try:
        wait_until(app, lambda: dialog.scrub_frames.quick_worker is None)
        track.set_view(0, track.maximum()*0.6)
        plan = dialog.session.plan
        start, span = track.view_start, track.view_span
        calls.clear()
        emitted = []
        track.sliderMoved.connect(emitted.append)
        track.setSliderDown(True)
        track.pointer_down = True
        dialog.seek_montage(2137, follow=False)
        dialog.sequence_position(2170)  # Playback clock must not become a seek either.
        assert not emitted  # Programmatic rounding must not feed back into input.
        track.setSliderDown(False)
        track.pointer_down = False
        calls.clear()
        pointer(QEvent.Type.MouseButtonPress, track.left_padding+20, Qt.MouseButton.LeftButton)
        for i in range(80):
            pointer(QEvent.Type.MouseMove, track.left_padding+20+
                (track.width()-track.left_padding-50)*(i/80))
        assert len(calls) <= 1  # A burst must not synchronously rebuild 80 selections.
        x = track.width()-31
        target = track.time_at(x)
        pointer(QEvent.Type.MouseButtonRelease, x, Qt.MouseButton.LeftButton)
        assert dialog.montage_seek.value() == target
        assert dialog.pending_navigation is None and not dialog.navigation_timer.isActive()
        assert (track.view_start, track.view_span) == (start, span)
        assert window.video_time.text().startswith(format_time(target/1000, 2)+' / ')
        frames = []
        dialog.preview_image.connect(lambda image: frames.append(track.pointer_down))
        loop = QEventLoop()
        count = [0]
        pointer(QEvent.Type.MouseButtonPress, track.left_padding+20, Qt.MouseButton.LeftButton)
        def move():
            count[0] += 1
            pointer(QEvent.Type.MouseMove, track.left_padding+20+
                (track.width()-track.left_padding-50)*(count[0]%60)/60)
            if count[0] >= 80:
                timer.stop()
                loop.quit()
        timer.setInterval(3)
        timer.timeout.connect(move)
        timer.start()
        loop.exec()
        assert sum(frames) >= 2  # Images must arrive while the button is still held.
        pointer(QEvent.Type.MouseButtonRelease, 90, Qt.MouseButton.LeftButton)
        assert dialog.session.plan == plan
        window.play_button.click()
        wait_until(app, dialog.playback.is_advancing, 20)
        pointer(QEvent.Type.MouseButtonPress, 100, Qt.MouseButton.LeftButton)
        pointer(QEvent.Type.MouseButtonRelease, 100, Qt.MouseButton.LeftButton)
        wait_until(app, dialog.playback.is_advancing, 20)
        assert dialog.sequence.desired and dialog.playback.want_play
    finally:
        timer.stop()
        if track.pointer_down:
            pointer(QEvent.Type.MouseButtonRelease, 90, Qt.MouseButton.LeftButton)
        finish(window, dialog, app)
