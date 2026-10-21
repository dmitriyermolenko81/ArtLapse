from pathlib import Path

import cv2
import numpy as np
import pytest

from sculptcut.screen_cleanup import project_screens, screen_kind, strip_screens


def test_fractional_source_tail_and_invalid_proxy(tmp_path: Path) -> None:
    from sculptcut.media import MediaError, discover, run_process
    from sculptcut.planning import simple_speed_plan
    from sculptcut.project import create_project
    from sculptcut.proxy import make_proxies, proxy_path
    from sculptcut.timelapse_cleanup import cleanup_ranges

    video = tmp_path / "fractional.mkv"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=128x96:r=30:d=2.1",
            "-c:v",
            "libx264",
            "-frames:v",
            "62",
            str(video),
        ]
    )
    root = tmp_path / "project"
    project = create_project(root, [video])
    make_proxies(root, project)
    capture = cv2.VideoCapture(str(proxy_path(root, project, project.sources[0])))
    try:
        assert capture.get(cv2.CAP_PROP_FRAME_COUNT) == 10
        assert project.sources[0].duration_us > 2_000_000
    finally:
        capture.release()
    plan = simple_speed_plan(project, 2)
    assert project_screens(root, project, None, plan) == []
    assert project_screens(root, project, None, plan) == []
    assert cleanup_ranges(root, project, plan, None) == []
    assert cleanup_ranges(root, project, plan, None) == []
    # A window entirely after the last CFR sample has no evidence to remove.
    tail = plan.model_copy(
        update={"decisions": [plan.decisions[0].model_copy(update={"source_start_us": 2_050_000})]}
    )
    assert project_screens(root, project, None, tail) == []
    assert cleanup_ranges(root, project, tail, None) == []
    too_long = project.model_copy(
        update={"sources": [project.sources[0].model_copy(update={"duration_us": 4_000_000})]}
    )
    with pytest.raises(MediaError, match="duration mismatch"):
        project_screens(root, too_long, None)
    with pytest.raises(MediaError, match="duration mismatch"):
        cleanup_ranges(root, too_long, simple_speed_plan(too_long, 2), None)


def test_reference_brush_grid_and_blank_screens() -> None:
    reference = cv2.imdecode(
        np.fromfile(Path(__file__).parent / "fixtures" / "brush-picker.png", dtype=np.uint8), 0
    )
    assert reference is not None
    assert screen_kind(reference.astype(np.uint8)) == "brush_picker"
    compact = cv2.imdecode(
        np.fromfile(Path(__file__).parent / "fixtures" / "compact-picker.png", dtype=np.uint8), 0
    )
    assert compact is not None
    assert screen_kind(compact.astype(np.uint8)) == "brush_picker"
    assert screen_kind(np.zeros((360, 640), np.uint8)) == "black_screen"
    assert screen_kind(np.full((360, 640), 255, np.uint8)) == "white_screen"
    sculpt = np.full((360, 640), 70, np.uint8)
    cv2.ellipse(sculpt, (320, 180), (85, 130), 0, 0, 360, 170, -1)
    assert screen_kind(sculpt) is None
    # A zoomed model divides the uniform canvas into left/right components.
    from sculptcut.shorts import model_box, viewport_box

    split = np.full((360, 640), 20, np.uint8)
    split[40:320, 80:560] = 70
    split[40:320, 240:380] = 170
    viewport = viewport_box(split)
    assert viewport is not None and viewport.x < 0.2 and viewport.width > 0.7
    box = model_box(split)
    assert box is not None and box.x < 240 / 640 and box.x + box.width > 380 / 640


def test_real_cleanup_cached_and_exported(tmp_path: Path) -> None:
    from sculptcut.media import discover, run_process
    from sculptcut.planning import build_plan, validate_plan
    from sculptcut.project import create_project
    from sculptcut.proxy import make_proxies
    from sculptcut.rendering import render, verify_output

    source = tmp_path / "screens.mp4"
    picker = Path(__file__).parent / "fixtures" / "brush-picker.png"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x464646:s=640x360:r=30:d=8",
            "-loop",
            "1",
            "-i",
            str(picker),
            "-filter_complex",
            "[0:v]drawbox=x=260:y=80:w=100:h=200:color=0xaaaaaa:t=fill,"
            "drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='between(t,1,2)',"
            "drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='between(t,3,4)'[base];"
            "[1:v]scale=640:360[menu];[base][menu]overlay=enable='between(t,5,6)':shortest=1",
            "-t",
            "8",
            "-c:v",
            "libx264",
            str(source),
        ]
    )
    root = tmp_path / "project"
    project = create_project(root, [source])
    make_proxies(root, project)
    ranges = project_screens(root, project, None)
    assert {r.reason for r in ranges} == {"black_screen", "white_screen", "brush_picker"}
    assert project_screens(root, project, None) == ranges
    plan = strip_screens(project, build_plan(project, [], 3), ranges)
    validate_plan(project, plan)
    assert plan.actual_frame_count == 90
    output = render(root, project, plan, tmp_path / "clean.mp4", encoder="libx264")
    verify_output(output, 90, 30)
    cap = cv2.VideoCapture(str(output))
    checked = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            assert screen_kind(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.uint8)) is None
            checked += 1
    finally:
        cap.release()
    assert checked == 90
