from pathlib import Path

import cv2
import numpy as np
from test_timeline import make_source

from sculptcut.domain import Project
from sculptcut.media import discover, probe, run_process
from sculptcut.planning import build_plan
from sculptcut.project import create_project
from sculptcut.rendering import export_size, render, verify_output


def test_native_canvas_rotation_and_mixed_sources() -> None:
    small = make_source(0, 0, 1).model_copy(update={"width": 1280, "height": 720})
    large = make_source(1, 1, 1).model_copy(update={"width": 2560, "height": 1440})
    assert export_size(Project(sources=[small, large])) == (2560, 1440)
    portrait = large.model_copy(update={"rotation": -90})
    assert export_size(Project(sources=[small, portrait])) == (1440, 2560)
    odd = small.model_copy(update={"width": 641, "height": 479})
    assert export_size(Project(sources=[odd])) == (642, 480)


def test_real_1440p_export_preserves_resolution_and_detail(tmp_path: Path) -> None:
    source = tmp_path / "detail.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=2560x1440:r=30:d=1",
            "-c:v",
            "libx264",
            "-crf",
            "0",
            str(source),
        ]
    )
    root = tmp_path / "project"
    project = create_project(root, [source])
    plan = build_plan(project, [], 1)
    result = render(root, project, plan, tmp_path / "result.mp4", encoder="libx264")
    metadata = probe(result)
    assert (metadata.width, metadata.height) == (2560, 1440)
    verify_output(result, 30, 30)
    frames = []
    for video, name in ((source, "before.png"), (result, "after.png")):
        image = tmp_path / name
        # Compare RGB pixels with each video's actual color matrix. OpenCV's
        # VideoCapture YUV conversion can ignore BT.709 and report a false loss
        # of detail when a BT.601 source is correctly converted for delivery.
        run_process([
            discover("ffmpeg"), "-v", "error", "-i", str(video),
            "-frames:v", "1", str(image),
        ])
        frame = cv2.imdecode(np.fromfile(image, dtype=np.uint8), cv2.IMREAD_COLOR)
        assert frame is not None
        frames.append(frame)
    before, after = frames
    # Detect detail loss even if someone later upscales a 720p intermediate.
    mse = np.mean((before.astype(np.float64) - after.astype(np.float64)) ** 2)
    assert mse < 16, f"Excessive first-frame distortion: MSE={mse}"
