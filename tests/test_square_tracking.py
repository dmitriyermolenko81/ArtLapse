from pathlib import Path

import cv2
import numpy as np
import pytest

from sculptcut.domain import ProjectSettings, Roi
from sculptcut.media import discover, run_process
from sculptcut.planning import build_plan
from sculptcut.project import create_project
from sculptcut.proxy import make_proxies
from sculptcut.rendering import render, verify_output
from sculptcut.shorts_layout import apply_square_layout
from sculptcut.square_tracking import horizontal_camera


def test_small_motion_and_isolated_bad_detection_keep_one_fixed_square() -> None:
    roi = Roi(x=0.3, y=0.2, width=0.4, height=0.6)
    samples = [
        (i * 200000, Roi(x=x, y=0.3, width=0.12, height=0.2))
        for i, x in enumerate([0.42, 0.43, 0.41, 0.02, 0.42, 0.43, 0.42])
    ]
    camera = horizontal_camera(roi, samples, ProjectSettings(), 0)
    assert len(camera.points) == 1
    assert camera.points[0].x == pytest.approx(0.5)
    assert camera.points[0].y == pytest.approx(0.5)
    assert camera.width == roi.width and camera.height == roi.height


def test_sustained_travel_pans_only_horizontally_without_scale_changes() -> None:
    roi = Roi(x=0.3, y=0.2, width=0.4, height=0.6)
    samples = [
        (i * 200000, Roi(x=0.12 + i * 0.03, y=0.3, width=0.12, height=0.2)) for i in range(20)
    ]
    camera = horizontal_camera(roi, samples, ProjectSettings(), 0)
    xs = [p.x for p in camera.points]
    assert xs == sorted(xs)
    assert max(abs(b - a) for a, b in zip(xs, xs[1:], strict=False)) <= 0.05
    assert len(set(p.y for p in camera.points)) == 1
    assert all(p.width is None and p.height is None for p in camera.points)
    assert min(xs) >= roi.width / 2 and max(xs) <= 1 - roi.width / 2


@pytest.mark.parametrize("canvas_mode", ["full", "detected", "manual"])
def test_real_square_pan_keeps_moving_object_visible(tmp_path: Path, canvas_mode: str) -> None:
    source = tmp_path / "moving.mp4"
    writer = cv2.VideoWriter(str(source), cv2.VideoWriter.fourcc(*"mp4v"), 30, (640, 360))
    assert writer.isOpened()
    for index in range(120):
        frame = np.full((360, 640, 3), 70, dtype=np.uint8)
        if canvas_mode != "full":
            frame[:, :80] = (0, 0, 255)
            frame[:, 560:] = (255, 0, 0)
        center = (
            round(80 + 480 * index / 119)
            if canvas_mode == "full"
            else round(135 + 370 * index / 119)
        )
        cv2.ellipse(frame, (center, 180), (42, 90), 0, 0, 360, (210, 210, 210), -1)
        writer.write(frame)
    writer.release()
    root = tmp_path / "project"
    project = create_project(root, [source])
    project = project.model_copy(
        update={
            "rois": {project.sources[0].id: Roi(x=0.125, y=0, width=0.75, height=1)}
            if canvas_mode == "manual"
            else {},
            "settings": ProjectSettings(
                preset="shorts",
                shorts_layout="square",
                shorts_square_rois={
                    project.sources[0].id: Roi(x=0.22, y=0, width=0.5625, height=1)
                },
            ),
        }
    )
    plan = apply_square_layout(project, build_plan(project, [], 2), root)
    camera = plan.decisions[0].subject_camera
    assert camera is not None and len(camera.points) > 1
    if canvas_mode != "full":
        assert camera.viewport.x >= 0.125
        assert camera.viewport.x + camera.viewport.width <= 0.875
        assert all(p.x - camera.width / 2 >= camera.viewport.x - 1e-9 for p in camera.points)
        assert all(
            p.x + camera.width / 2 <= camera.viewport.x + camera.viewport.width + 1e-9
            for p in camera.points
        )
    make_proxies(root, project)
    for preview in (False, True):
        output = render(
            root,
            project,
            plan,
            tmp_path / f"tracked-{preview}.mp4",
            encoder="libx264",
            preview=preview,
        )
        verify_output(output, 60, 30)
        run_process(
            [discover("ffmpeg"), "-v", "error", "-xerror", "-i", str(output), "-f", "null", "-"]
        )
        capture = cv2.VideoCapture(str(output))
        try:
            for _index in range(60):
                ok, image = capture.read()
                assert ok
                width = image.shape[1]
                top = (image.shape[0] - width) // 2
                region = image[top : top + width]
                assert not np.any((region[:, :, 2] > 160) & (region[:, :, 1] < 40))
                assert not np.any((region[:, :, 0] > 160) & (region[:, :, 1] < 40))
                mask = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY) > 150
                rows, cols = np.where(mask)
                assert len(cols) > width * width * 0.04
                assert cols.min() > width * 0.02
                assert cols.max() < width * 0.98
                assert rows.min() > 0 and rows.max() < width - 1
                assert np.max(np.abs(image[10 : top - 10].astype(int) - 24)) <= 4
        finally:
            capture.release()
