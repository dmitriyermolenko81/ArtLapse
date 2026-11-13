"""All-intra preview copies preserve clocks and are reused without rewriting the video."""
from hashlib import sha256
from threading import Event

import cv2
import pytest
from PySide6.QtWidgets import QApplication, QWidget
from test_episode_panel import wait_until
from test_episode_roll import roll_movie as roll_movie

from sculptcut.episode_images import image_key
from sculptcut.media import Cancelled
from sculptcut.quick_seek import build_copy
from sculptcut.rendering import verify_output
from sculptcut.scrub_frames import ScrubFrames


def test_copy_decodes_and_serves_original_keys_without_changing_original(roll_movie, tmp_path):
    app = QApplication.instance() or QApplication([])
    video = roll_movie[-1]
    before = sha256(video.read_bytes()).digest()
    copy = build_copy(video, tmp_path/'seek.mp4', Event())
    stamp = copy.stat().st_mtime_ns
    assert build_copy(video, copy, Event()) == copy and copy.stat().st_mtime_ns == stamp
    captures = [cv2.VideoCapture(str(p)) for p in (video, copy)]
    try:
        assert (captures[0].get(cv2.CAP_PROP_FRAME_COUNT)
                == captures[1].get(cv2.CAP_PROP_FRAME_COUNT))
        assert captures[0].get(cv2.CAP_PROP_FPS) == captures[1].get(cv2.CAP_PROP_FPS)
        assert [(c.get(3), c.get(4)) for c in captures][0] == (
            captures[1].get(3), captures[1].get(4))
        verify_output(copy, round(captures[0].get(cv2.CAP_PROP_FRAME_COUNT)),
                      round(captures[0].get(cv2.CAP_PROP_FPS)))
    finally:
        for capture in captures:
            capture.release()
    owner = QWidget()
    frames = ScrubFrames(owner)
    frames.worker.use_proxy(video.resolve(), copy)
    received = []
    frames.ready.connect(lambda key, image: received.append(key))
    try:
        key = image_key(video, 2200000)
        frames.request(key, forward=True)
        wait_until(app, lambda: bool(received))
        assert received == [key]
        assert sha256(video.read_bytes()).digest() == before
    finally:
        frames.shutdown()
        wait_until(app, lambda: frames.cleaned)
        owner.close()
    cancel = Event()
    cancel.set()
    with pytest.raises(Cancelled):
        build_copy(video, tmp_path/'cancelled.mp4', cancel)
    assert not list(tmp_path.glob('*.pending.mp4'))
