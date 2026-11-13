"""Rapid timeline navigation has one pending request and never seeks the UI player."""
from time import monotonic

import cv2
import numpy as np
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QWidget
from test_cursor_playback import finish, open_editor
from test_episode_panel import wait_until
from test_episode_roll import roll_movie as roll_movie

from sculptcut.episode_images import image_key
from sculptcut.scrub_frames import ScrubFrames


def test_nearby_forward_and_backward_clicks_show_the_exact_encoded_frame(tmp_path):
    app = QApplication.instance() or QApplication([])
    path = tmp_path/'numbered.avi'
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'MJPG'), 25, (96, 64))
    assert writer.isOpened()
    for index in range(90):
        writer.write(np.full((64, 96, 3), index*2, dtype=np.uint8))
    writer.release()
    owner = QWidget()
    frames = ScrubFrames(owner)
    received = []
    frames.ready.connect(lambda key, image: received.append((key, image)))
    try:
        for index in (20, 22, 25, 28, 5, 7, 65, 66, 65):
            key = image_key(path, index*40_000)
            received.clear()
            frames.request(key, forward=True)
            wait_until(app, lambda: bool(received))
            assert received[-1][0] == key
            assert abs(received[-1][1].pixelColor(20, 20).red()-index*2) <= 1
    finally:
        frames.shutdown()
        wait_until(app, lambda: frames.cleaned)
        owner.close()

def test_drag_burst_keeps_latest_frame_without_native_seeks_or_late_close_callbacks(
    roll_movie, monkeypatch,
):
    app = QApplication.instance() or QApplication([])
    root, _, _, _, video = roll_movie
    window, dialog = open_editor(root, video, app)
    received = []
    ticks = []
    timer = QTimer()
    try:
        def forbidden(*args):
            raise AssertionError('Paused timeline must not touch the native playback decoder')
        monkeypatch.setattr(dialog.playback.player, 'setPosition', forbidden)
        monkeypatch.setattr(dialog.playback.player, 'setSource', forbidden)
        dialog.scrub_frames.ready.connect(lambda key, image: received.append(key))
        original = dialog.session.plan
        counter = [0]
        loop = QEventLoop()
        def move():
            ticks.append(monotonic())
            row = counter[0] % len(dialog.montage_seek.blocks)
            dialog.seek_montage(dialog.montage_seek.blocks[row][1]+100)
            counter[0] += 1
            if counter[0] == 100:
                timer.stop()
                loop.quit()
        timer.setInterval(5)
        timer.timeout.connect(move)
        timer.start()
        loop.exec()
        wanted = dialog.scrub_frames.wanted
        wait_until(app, lambda: bool(received) and received[-1] == wanted)
        assert max(b-a for a, b in zip(ticks, ticks[1:], strict=False)) < 0.25
        assert dialog.session.plan == original
        assert len(dialog.scrub_frames.cache) <= 24
        received.clear()
        dialog.scrub_frames.request(wanted)
        assert received == [wanted]  # Cache hit is synchronous.
        dialog.seek_montage(dialog.montage_seek.blocks[0][1]+300)
    finally:
        timer.stop()
        monkeypatch.undo()
        finish(window, dialog, app)
        wait_until(app, lambda: dialog.scrub_frames.cleaned)
