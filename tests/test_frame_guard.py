"""Real encoded clips exercise conservative, timestamp-preserving flash repair."""
from threading import Event

import cv2
import numpy as np
import pytest

from sculptcut.frame_guard import flash_filter
from sculptcut.media import Cancelled
from sculptcut.rendering import normalize_timing, verify_output


@pytest.mark.parametrize('bad,expected', [
    ([20], [20]), (list(range(20, 35)), []),
    (list(range(5, 80, 10)), []), ([0, 89], []), ([], []), (list(range(90)), []),
])
def test_real_flash_repair_preserves_uncertain_runs_and_frame_clock(tmp_path, bad, expected,
                                                                   caplog):
    source = tmp_path/'source.mp4'
    writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*'mp4v'), 30, (160, 96))
    assert writer.isOpened()
    clean = np.full((96, 160, 3), 70, np.uint8)
    cv2.ellipse(clean, (80, 48), (25, 37), 0, 0, 360, (170, 170, 170), -1)
    try:
        for i in range(90):
            writer.write(np.zeros_like(clean) if i in bad else clean)
    finally:
        writer.release()
    filters, repaired = flash_filter(source, 30, Event())
    assert repaired == expected
    if bad and not expected:
        assert 'preserving' in caplog.text
        assert filters == 'settb=1/30,setpts=N'
    output = tmp_path/'output.mp4'
    assert normalize_timing(source, output, 30, None, screen_guard=True,
                            encoder='libx264') == expected
    verify_output(output, 90, 30)
    capture = cv2.VideoCapture(str(output))
    frames = []
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            frames.append(frame)
    finally:
        capture.release()
    assert len(frames) == 90
    if expected:
        assert frames[20].mean() > 30
    elif bad:
        assert frames[bad[len(bad)//2]].mean() < 5


def test_flash_check_cancel_and_undecodable_input_still_fail(tmp_path):
    from sculptcut.media import MediaError

    cancel = Event()
    cancel.set()
    with pytest.raises(Cancelled):
        flash_filter(tmp_path/'missing.mp4', 30, cancel)
    with pytest.raises(MediaError, match='Cannot decode'):
        flash_filter(tmp_path/'missing.mp4', 30, None)
