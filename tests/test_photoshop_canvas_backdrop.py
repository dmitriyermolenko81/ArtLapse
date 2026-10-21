from pathlib import Path

import cv2
import numpy as np

from sculptcut.domain import Roi
from sculptcut.media import discover, run_process
from sculptcut.photoshop import centered_still_cameras, drawing_and_canvas_box
from sculptcut.photoshop_beats import canvas_background
from sculptcut.rendering import verify_output
from sculptcut.subject_camera import camera_filter, camera_preview


def test_variable_canvas_shapes_fill_portrait_without_chrome_or_camera_jumps(
    tmp_path: Path,
) -> None:
    boxes = [Roi(x=0.42, y=0.25, width=0.12, height=0.45),
             Roi(x=0.32, y=0.2, width=0.24, height=0.6)]
    canvases = [Roi(x=0.05, y=0.2, width=0.9, height=0.55),
                Roi(x=0.2, y=0.1, width=0.6, height=0.8)]
    cameras = centered_still_cameras(list(enumerate(boxes)), 640, 360, 1.25, canvases[0])
    for i, (box, canvas) in enumerate(zip(boxes, canvases, strict=True)):
        frame = np.full((360, 640, 3), (0, 255, 0), np.uint8)
        x, y, w, h = (round(canvas.x*640), round(canvas.y*360),
                      round(canvas.width*640), round(canvas.height*360))
        frame[y:y+h, x:x+w] = (180, 140, 100)
        x, y, w, h = (round(box.x*640), round(box.y*360),
                      round(box.width*640), round(box.height*360))
        frame[y:y+h, x:x+w] = (0, 0, 240)
        camera = cameras[i].model_copy(update={
            "viewport": canvas, "background_rgb": canvas_background(frame, canvas),
        })
        preview = camera_preview(frame, camera, 0, 360, 640)
        assert np.max(np.abs(preview[0].astype(int) - [180, 140, 100])) <= 1
        assert np.max(np.abs(preview[-1].astype(int) - [180, 140, 100])) <= 1
        image = tmp_path / f"{i}.png"
        cv2.imencode('.png', frame)[1].tofile(str(image))
        output = tmp_path / f"{i}.mp4"
        run_process([discover('ffmpeg'), '-v', 'error', '-loop', '1', '-i', str(image),
                     '-vf', camera_filter(camera, 0, 1, 640, 360, 360, 640)+'setsar=1',
                     '-frames:v', '10', '-r', '30', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                     str(output)])
        verify_output(output, 10, 30)
        reader = cv2.VideoCapture(str(output))
        ok, result = reader.read()
        reader.release()
        assert ok
        assert np.max(np.abs(result[0].astype(int) - [180, 140, 100])) < 8
        assert not np.any((result[:, :, 1] > 220) & (result[:, :, 2] < 40))
        for candidate in (preview, result):
            ys, xs = np.where((candidate[:, :, 2] > 180) & (candidate[:, :, 0] < 60))
            assert max(np.ptp(xs)/360, np.ptp(ys)/640) > 0.76
            assert abs((xs.max()+xs.min())/720 - 0.5) < 0.015
            assert abs((ys.max()+ys.min())/1280 - 0.5) < 0.015


def test_canvas_edge_connected_to_artwork_is_not_a_landscape_subject() -> None:
    frame = np.full((360, 640, 3), 40, np.uint8)
    frame[24:344, 10:636] = (190, 160, 145)
    frame[24:344, 240:400] = (70, 60, 60)
    # Downsampled scrollbar runs along the canvas and touches the drawing.
    frame[343:344, 10:636] = (80, 80, 80)
    box, canvas = drawing_and_canvas_box(frame)
    assert box is not None and canvas is not None
    assert box.width < 0.27
    assert box.height > 0.85
    assert abs(box.x + box.width/2 - 0.5) < 0.01
