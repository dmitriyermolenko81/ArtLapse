"""Measure the rendered finale's motion rather than only inspecting its expression."""
from threading import Event

import cv2
import numpy as np
import pytest

from sculptcut.bookends import bookend_filter
from sculptcut.media import discover, run_process
from sculptcut.rendering import verify_output


@pytest.mark.parametrize('width,height', [(320, 180), (180, 320), (960, 540)])
def test_final_zoom_moves_subpixel_with_soft_start_and_stop(tmp_path, width, height):
    line = round(width*0.3125)
    image = np.full((height, width, 3), 255, np.uint8)
    image[:, line-1:line+2] = 0
    source = tmp_path/'line.png'
    encoded, png = cv2.imencode('.png', image)
    assert encoded
    source.write_bytes(png.tobytes())
    output = tmp_path/'zoom.mkv'
    graph = bookend_filter(90, 60, 30, width, height)
    run_process([discover('ffmpeg'), '-v', 'error', '-loop', '1', '-framerate', '30',
        '-i', str(source), '-vf', graph, '-frames:v', '90', '-c:v', 'ffv1',
        '-pix_fmt', 'yuv444p', str(output)], Event())
    verify_output(output, 90, 30)
    reader = cv2.VideoCapture(str(output))
    positions = []
    try:
        while True:
            ok, frame = reader.read()
            if not ok:
                break
            a, b = line-30, line+30
            weights = 255-frame[height//2, a:b, 0].astype(float)
            positions.append(float(np.dot(np.arange(a, b), weights)/weights.sum()))
    finally:
        reader.release()
    motion = np.diff(positions)
    assert np.max(np.abs(motion[:30])) < 0.02  # Body and first tail frame stay unchanged.
    assert abs(positions[-1]-(width/2+(line-width/2)*1.06)) < 0.15
    assert np.max(np.abs(motion)) < 0.25*width/320  # No whole-pixel crop jumps.
    assert np.max(motion) < 0.06
    assert abs(motion[30]) < 0.06 and abs(motion[-1]) < 0.06
