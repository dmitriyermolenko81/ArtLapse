"""A brief close-up must not turn the remainder of a square shot into empty canvas."""

from pathlib import Path

import cv2
import numpy as np
import pytest

from sculptcut.domain import ProjectSettings, Roi
from sculptcut.media import discover, run_process
from sculptcut.planning import build_plan
from sculptcut.project import create_project
from sculptcut.rendering import render, verify_output
from sculptcut.shorts_layout import apply_square_layout
from sculptcut.square_tracking import safe_viewport, square_viewport


def test_blank_background_fragment_cannot_replace_a_canvas_containing_the_model() -> None:
    gray = np.full((360,640),20,np.uint8)
    gray[40:320,80:560] = 70
    # A foreground close-up hides the left part of the flat background.
    gray[40:320,80:330] = 180
    canvas = Roi(x=80/640,y=40/360,width=480/640,height=280/360)
    square = Roi(x=180/640,y=40/360,width=280/640,height=280/360)
    detected = square_viewport(gray,square,canvas)
    assert detected.x <= canvas.x and detected.x+detected.width >= canvas.x+canvas.width
    subject = Roi(x=.35,y=.3,width=.12,height=.4)
    faulty = Roi(x=.52,y=canvas.y,width=.35,height=canvas.height)
    common = safe_viewport([canvas,canvas,faulty,canvas],square,[subject])
    assert common.x <= subject.x
    assert common.x+common.width >= subject.x+subject.width


@pytest.mark.parametrize("preset", ["shorts", "music_shorts"])
def test_real_closeup_does_not_blank_other_frames_of_square_episode(
    tmp_path: Path, preset: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "closeup.mp4"
    writer = cv2.VideoWriter(str(video),cv2.VideoWriter_fourcc(*"mp4v"),30,(640,360))
    assert writer.isOpened()
    for index in range(60):
        frame = np.full((360,640,3),20,np.uint8)
        frame[40:320,80:560] = 70
        cv2.ellipse(frame,(265,180),(42,90),0,0,360,(20,210,20),-1)
        if 24 <= index < 30:
            frame[40:320,80:330] = (20,210,20)
        writer.write(frame)
    writer.release()
    root = tmp_path / "project"
    project = create_project(root,[video])
    source_id = project.sources[0].id
    square = Roi(x=180/640,y=40/360,width=280/640,height=280/360)
    project = project.model_copy(update={"settings":ProjectSettings.model_validate({
        "preset":preset,"shorts_layout":"square","shorts_square_rois":{source_id:square},
    })})
    raw = build_plan(project,[],2)
    fixed = apply_square_layout(project,raw,root)
    assert fixed.actual_frame_count == 60
    assert [(d.source_start_us,d.source_end_us,d.output_frame_count) for d in fixed.decisions] == [
        (d.source_start_us,d.source_end_us,d.output_frame_count) for d in raw.decisions]
    camera = fixed.decisions[0].subject_camera
    assert camera is not None and camera.viewport.x < .3
    assert camera.width == square.width and camera.height == square.height
    monkeypatch.setattr("sculptcut.rendering.export_size",lambda *args:(180,320))
    output = render(root,project,fixed,tmp_path / "fixed.mp4",encoder="libx264")
    verify_output(output,60,30)
    run_process([discover("ffmpeg"),"-v","error","-xerror","-i",str(output),"-f","null","-"])
    capture = cv2.VideoCapture(str(output))
    count = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            green = frame[:,:,1].astype(float)
            mask = (green>110)&(green>frame[:,:,0]*1.5)&(green>frame[:,:,2]*1.5)
            assert np.count_nonzero(mask) > 180*180*.03
            count += 1
    finally:
        capture.release()
    assert count == 60
