"""A toolbar opening during a shot must not become the tracked sculpt."""

from pathlib import Path

import cv2
import numpy as np
import pytest

from sculptcut.domain import ProjectSettings, Roi
from sculptcut.media import discover, run_process
from sculptcut.planning import RenderDecision, build_plan, validate_plan
from sculptcut.project import create_project
from sculptcut.rendering import render, verify_output
from sculptcut.shorts import reframe_shorts, shared_viewport, tracking_viewport


@pytest.mark.parametrize("edge", ["top", "bottom", "left", "right"])
def test_changed_panel_is_distinct_from_a_large_model(edge: str) -> None:
    gray = np.full((360, 640), 20, np.uint8)
    gray[40:320, 80:560] = 70
    old = Roi(x=80/640, y=40/360, width=480/640, height=280/360)
    rectangles = {
        "top": (80, 40, 560, 80), "bottom": (80, 280, 560, 320),
        "left": (80, 40, 120, 320), "right": (520, 40, 560, 320),
    }
    x, y, r, b = rectangles[edge]
    gray[y:b, x:r] = 30
    left, top, right, bottom = 80, 40, 560, 320
    if edge == "top":
        top = b
    elif edge == "bottom":
        bottom = y
    elif edge == "left":
        left = r
    else:
        right = x
    current = Roi(x=left/640, y=top/360, width=(right-left)/640, height=(bottom-top)/360)
    assert tracking_viewport(gray, current, old) == current
    # A large foreground is not a thin edge panel, even if background detection
    # mistakenly returns the same inset. Keep its visible part in the viewport.
    gray[40:320, 80:560] = 150
    gray[140:220, 240:400] = 70
    assert tracking_viewport(gray, current, old) == old


def test_common_crop_does_not_cut_a_subject_in_another_frame() -> None:
    full = Roi(x=.1, y=.1, width=.8, height=.8)
    inset = Roi(x=.1, y=.2, width=.8, height=.7)
    subjects = [Roi(x=.3, y=.12, width=.3, height=.6)]
    assert shared_viewport([full, full, inset], subjects) == full


@pytest.mark.parametrize("preserve_selection", [False, True])
def test_real_toolbar_change_keeps_model_and_musical_frame_counts(
    tmp_path: Path, preserve_selection: bool,
) -> None:
    from sculptcut.domain import CameraPoint, SubjectCamera

    video = tmp_path / "toolbar.mp4"
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 "color=c=red:s=640x360:r=30:d=2,"
                 "drawbox=x=80:y=40:w=480:h=280:color=0x464646:t=fill,"
                 "drawbox=x=80:y=40:w=480:h=40:color=blue:t=fill:"
                 "enable='gte(t,1)*lt(t,1.37)',"
                 "drawbox=x=270:y=135:w=80:h=160:color=lime:t=fill",
                 "-c:v", "libx264", str(video)])
    root = tmp_path / "project"
    project = create_project(root, [video])
    project = project.model_copy(update={"settings": ProjectSettings(preset="music_shorts")})
    viewport = Roi(x=80/640, y=40/360, width=480/640, height=280/360)
    old_camera = SubjectCamera(viewport=viewport, width=.3, height=viewport.height, background=70,
                              points=[CameraPoint(time_us=0, x=.5, y=.5)])
    decisions = [RenderDecision(source_id=project.sources[0].id,
                               source_start_us=i*1_000_000, source_end_us=(i+1)*1_000_000,
                               action="SPEED", output_frame_count=30,
                               provenance="user:process_story", crop=viewport,
                               subject_camera=old_camera) for i in range(2)]
    plan = build_plan(project, [], 2).model_copy(update={
        "decisions": decisions, "actual_frame_count": 60, "target_frame_count": 60,
        "settings": {"preset":"music_shorts", "story":"music_episodes",
                     "width":180, "height":320},
    })
    fixed = reframe_shorts(root, project, plan, None, preserve_selection=preserve_selection)
    validate_plan(project, fixed)
    before = [(d.source_start_us, d.source_end_us, d.output_frame_count) for d in plan.decisions]
    after = [(d.source_start_us, d.source_end_us, d.output_frame_count) for d in fixed.decisions]
    assert after == before
    camera = fixed.decisions[1].subject_camera
    assert camera is not None and camera.viewport.y >= 79/360
    assert camera.width < viewport.width / 2
    for preview in (False, True):
        output = render(root, project, fixed, tmp_path / f"fixed-{preview}.mp4",
                        encoder="libx264", preview=preview)
        verify_output(output, 60, 30)
        capture = cv2.VideoCapture(str(output))
        count = 0
        try:
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                blue, green, red = (frame[:, :, i].astype(float) for i in range(3))
                assert np.count_nonzero((blue > 100) & (blue > green*1.5) & (blue > red*1.5)) == 0
                assert np.count_nonzero((green > 100) & (green > blue*1.5)) > frame.size / 100
                count += 1
        finally:
            capture.release()
        assert count == 60
