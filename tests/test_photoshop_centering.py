import numpy as np

from sculptcut.domain import Roi
from sculptcut.photoshop import centered_camera, centered_still_cameras
from sculptcut.subject_camera import camera_preview


def test_still_source_zoom_keeps_drawing_center_and_screen_size() -> None:
    small = Roi(x=0.15, y=0.3, width=0.08, height=0.4)
    large = Roi(x=0.6, y=0.1, width=0.16, height=0.8)
    samples = [(0, small), (1_000_000, small), (2_000_000, large), (3_000_000, large)]
    viewport = Roi(x=0.02, y=0.02, width=0.96, height=0.96)
    cameras = centered_still_cameras(samples, 640, 360, 1.25, viewport)
    assert cameras[0] == cameras[1_000_000]
    assert cameras[2_000_000] == cameras[3_000_000]
    heights = []
    for timestamp, box in samples:
        frame = np.full((360, 640, 3), 210, dtype=np.uint8)
        left, top = round(box.x * 640), round(box.y * 360)
        frame[top:top+round(box.height*360), left:left+round(box.width*640)] = (0, 0, 240)
        image = camera_preview(frame, cameras[timestamp], timestamp, 360, 640)
        ys, xs = np.where((image[:, :, 2] > 200) & (image[:, :, 0] < 50))
        assert abs((xs.min() + xs.max()) / 2 / 360 - 0.5) < 0.01
        assert abs((ys.min() + ys.max()) / 2 / 640 - 0.5) < 0.01
        heights.append((ys.max() - ys.min()) / 640)
    assert min(heights) > 0.77
    assert max(heights) - min(heights) < 0.01


def test_small_detection_jitter_does_not_move_or_zoom_camera() -> None:
    samples = [(i * 100_000, Roi(x=0.4+0.004*np.sin(i), y=0.2,
                               width=0.15, height=0.6)) for i in range(30)]
    camera = centered_camera(samples, 10, 640, 360, 1.25,
                             Roi(x=0, y=0, width=1, height=1))
    assert len(camera.points) == 1
    assert abs(camera.height - 0.75) < 1e-9
    assert camera.width < 0.25


def test_changing_drawing_and_detection_gaps_cannot_animate_camera() -> None:
    samples = [(0, Roi(x=0.1, y=0.2, width=0.12, height=0.4)),
               (100_000, None),
               (200_000, Roi(x=0.55, y=0.1, width=0.3, height=0.8))]
    camera = centered_camera(samples, 30, 640, 360, 1.25,
                             Roi(x=0, y=0, width=1, height=1))
    assert len(camera.points) == 1
    point = camera.points[0]
    for _, box in samples:
        if box is None:
            continue
        assert point.x - camera.width / 2 <= box.x
        assert point.x + camera.width / 2 >= box.x + box.width
        assert point.y - camera.height / 2 <= box.y
        assert point.y + camera.height / 2 >= box.y + box.height
    from sculptcut.subject_camera import camera_filter

    expression = camera_filter(camera, 0, 1, 640, 360, 1080, 1920)
    assert "clip((t-" not in expression
