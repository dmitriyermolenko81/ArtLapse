from pathlib import Path

import cv2
import numpy as np
import pytest
from test_timeline import make_source

from sculptcut.domain import Project, ProjectSettings, Roi
from sculptcut.idle import IdleRange
from sculptcut.planning import RenderDecision, build_plan, validate_plan
from sculptcut.segmentation import Segment
from sculptcut.shorts import (
    highlight_plan,
    locked_shorts_camera,
    model_box,
    portrait_box,
    reframe_shorts,
    scene_cut_frames,
    sculpt_footprint,
    stabilized_points,
    subject_extent,
    sustained_screens,
    tracked_subjects,
    viewport_box,
    viewport_shorts_camera,
)


def test_short_detection_miss_keeps_model_center_and_size_outlier_does_not_set_zoom() -> None:
    model = Roi(x=0.25, y=0.3, width=0.2, height=0.4)
    outlier = Roi(x=0.1, y=0.1, width=0.8, height=0.8)
    viewport = Roi(x=0, y=0, width=1, height=1)
    samples: list[tuple[int, Roi | None]] = [(0, None)] + [
        (i, outlier if i == 5 else None if i == 4 else model) for i in range(1, 12)
    ]
    tracked = tracked_subjects(samples, viewport, False)
    assert tracked[0][1] == model
    assert tracked[4][1] == model
    assert tracked[5][1] == outlier
    assert tracked[6][1] == model
    assert subject_extent(tracked, 0.9) == pytest.approx((0.2, 0.4))
    assert subject_extent(tracked, 1) == pytest.approx((0.8, 0.8))
    assert all(box == viewport for _, box in tracked_subjects(samples, viewport, True))


def test_shorts_camera_rejects_frame_jitter_and_keeps_a_real_pan() -> None:
    tracked = []
    for index in range(180):
        x = 0.25 + index * 0.4 / 179 + (0.015 if index % 2 else -0.015)
        y = 0.5 + (0.008 if index % 2 else -0.008)
        if index == 90:
            x = 0.85
        tracked.append(
            (round(index * 1_000_000 / 30), Roi(x=x - 0.05, y=y - 0.05, width=0.1, height=0.1))
        )
    points = stabilized_points(tracked, 30)
    assert len(points) == len(tracked)
    assert points[-1].x - points[0].x > 0.38
    assert max(abs(b.x - a.x) for a, b in zip(points, points[1:], strict=False)) < 0.015
    assert max(p.y for p in points) - min(p.y for p in points) < 0.001


def test_sculpt_footprint_includes_separate_parts_but_ignores_corner_widgets() -> None:
    gray = np.full((360, 640), 72, dtype=np.uint8)
    viewport = Roi(x=0.1, y=0.1, width=0.75, height=0.8)
    cv2.rectangle(gray, (140, 100), (215, 300), 35, -1)
    cv2.rectangle(gray, (225, 145), (330, 295), 30, -1)
    cv2.rectangle(gray, (65, 38), (105, 65), 20, -1)
    cv2.rectangle(gray, (500, 40), (535, 68), 20, -1)
    box = sculpt_footprint(gray, viewport)
    assert box is not None
    assert 0.21 < box.x < 0.23
    assert 0.50 < box.x + box.width < 0.53
    assert 0.27 < box.y < 0.29
    assert 0.82 < box.y + box.height < 0.85


def test_sculpt_footprint_selects_one_of_two_models_and_keeps_that_side() -> None:
    viewport = Roi(x=0, y=0, width=1, height=1)
    gray = np.full((540, 960), 95, dtype=np.uint8)
    cv2.rectangle(gray, (100, 80), (430, 370), 35, -1)
    cv2.rectangle(gray, (550, 80), (900, 370), 30, -1)
    selected = sculpt_footprint(gray, viewport)
    assert selected is not None
    assert selected.x > 0.4
    assert selected.x + selected.width > 0.9

    # The other model becomes larger, but this shot stays on the same side.
    cv2.rectangle(gray, (60, 70), (440, 380), 35, -1)
    following = sculpt_footprint(gray, viewport, selected)
    assert following is not None
    assert following.x > 0.4
    assert following.x + following.width > 0.9


def test_scene_changes_create_spaced_fixed_shots() -> None:
    changes = [(30, 0.02), (77, 0.019), (81, 0.026), (82, 0.016), (125, 0.005)]
    assert scene_cut_frames(changes, 174, 30) == [81]
    assert scene_cut_frames([(81, 0.005)], 174, 30) == []


def test_ordinary_shorts_camera_stays_on_canvas_and_moves_only_sideways() -> None:
    viewport = Roi(x=0.1, y=0.08, width=0.78, height=0.82)
    aspect = (9 / 16) * (1080 / 1920)
    center = viewport_shorts_camera(viewport, [], aspect, 0, 70)
    right = viewport_shorts_camera(
        viewport, [Roi(x=0.62, y=0.2, width=0.12, height=0.5)], aspect, 1_000_000, 70
    )
    assert center.width == pytest.approx(viewport.height * aspect)
    assert center.height == pytest.approx(viewport.height)
    assert center.points[0].x == pytest.approx(viewport.x + viewport.width / 2)
    assert right.points[0].x > center.points[0].x
    for camera in (center, right):
        assert len(camera.points) == 1
        assert camera.points[0].y == pytest.approx(viewport.y + viewport.height / 2)
        assert viewport.x <= camera.points[0].x - camera.width / 2
        assert camera.points[0].x + camera.width / 2 <= viewport.x + viewport.width
        assert viewport.y <= camera.points[0].y - camera.height / 2 + 1e-9
        assert camera.points[0].y + camera.height / 2 <= viewport.y + viewport.height + 1e-9


def test_ordinary_shorts_camera_reaches_both_viewport_edges() -> None:
    viewport = Roi(x=0.1, y=0.08, width=0.78, height=0.82)
    aspect = 0.316
    left_box = Roi(x=0.1, y=0.2, width=0.2, height=0.5)
    right_box = Roi(x=0.7, y=0.2, width=0.18, height=0.5)
    for box, edge in ((left_box, "left"), (right_box, "right")):
        camera = viewport_shorts_camera(viewport, [box], aspect, 0, 70)
        x = camera.points[0].x
        if edge == "left":
            assert x - camera.width / 2 == pytest.approx(viewport.x)
        else:
            assert x + camera.width / 2 == pytest.approx(viewport.x + viewport.width)
        assert x - camera.width / 2 <= box.x
        assert x + camera.width / 2 >= box.x + box.width


def test_ordinary_shorts_popup_outlier_does_not_widen_camera_to_full_canvas() -> None:
    viewport = Roi(x=0.106, y=0.122, width=0.745, height=0.796)
    sculpt = [
        Roi(x=0.17 + (index % 3) * 0.004, y=0.348, width=0.14, height=0.393) for index in range(20)
    ]
    popup_detections = [
        Roi(x=viewport.x, y=viewport.y, width=viewport.width, height=0.43) for _ in range(2)
    ]
    camera = viewport_shorts_camera(
        viewport,
        sculpt + popup_detections,
        aspect=(9 / 16) * (1080 / 1920),
        time_us=0,
        background=72,
        ignore_isolated_outliers=True,
    )

    assert camera.width < viewport.width / 2
    assert camera.points[0].x + camera.width / 2 < viewport.x + viewport.width / 2


def test_process_camera_does_not_zoom_out_to_follow_source_zoom() -> None:
    viewport = Roi(x=0.1, y=0.1, width=0.75, height=0.8)
    tracked = [
        (0, Roi(x=0.2, y=0.2, width=0.15, height=0.4)),
        (1_000_000, Roi(x=0.25, y=0.1, width=0.6, height=0.8)),
    ]
    camera = locked_shorts_camera(
        tracked, [box for _, box in tracked], viewport, 30, 0.316, 1.25, 72, (0.26, 0.41)
    )
    assert len(camera.points) == 1
    assert camera.width == pytest.approx(0.41)
    assert camera.height == pytest.approx(camera.width / 0.316)


def test_brief_screen_detections_do_not_fragment_process_episodes() -> None:
    flashes = [
        IdleRange(source_id="a", start_us=0, end_us=200_000, reason="brush_picker"),
        IdleRange(source_id="a", start_us=180_000, end_us=400_000, reason="brush_picker"),
        IdleRange(source_id="a", start_us=1_000_000, end_us=1_600_000, reason="black_screen"),
        IdleRange(source_id="a", start_us=1_500_000, end_us=2_300_000, reason="black_screen"),
        IdleRange(source_id="b", start_us=0, end_us=300_000, reason="brush_picker"),
    ]
    assert [(r.source_id, r.start_us, r.end_us) for r in sustained_screens(flashes, 1_000_000)] == [
        ("a", 1_000_000, 2_300_000)
    ]


def test_highlights_cover_chronology_and_exact_minute() -> None:
    project = Project(
        sources=[make_source(0, 0, 13_000_000_000)], settings=ProjectSettings(preset="shorts")
    )
    segments = [Segment(source_id="0", start_us=0, end_us=13_000_000_000, kind="UNKNOWN", score=0)]
    plan = highlight_plan(project, build_plan(project, [], 60), segments)
    validate_plan(project, plan)
    assert plan.actual_frame_count == 1800
    kept = [d for d in plan.decisions if d.action != "DROP"]
    assert len(kept) == 20
    assert kept[0].source_start_us == 0
    assert kept[-1].source_end_us == 13_000_000_000
    assert kept[-1].output_frame_count == 150
    assert all(
        d.provenance == "user:shorts_summary_omission" for d in plan.decisions if d.action == "DROP"
    )


def test_model_centering_and_safe_rejection() -> None:
    frame = np.full((360, 640), 70, dtype=np.uint8)
    frame[120:230, 340:395] = 170
    box = model_box(frame)
    assert box is not None
    crop = portrait_box(box, 640, 360, False)
    assert crop.x <= box.x and crop.y <= box.y
    assert crop.x + crop.width >= box.x + box.width
    assert crop.y + crop.height >= box.y + box.height
    assert abs(crop.width * 640 / (crop.height * 360) - 9 / 16) < 0.001
    assert abs((crop.x + crop.width / 2) - (box.x + box.width / 2)) < 0.001
    assert model_box(np.full((360, 640), 70, dtype=np.uint8)) is None


def test_viewport_without_subject_excludes_panels() -> None:
    frame = np.full((360, 640), 20, dtype=np.uint8)
    frame[40:320, 80:560] = 70
    viewport = viewport_box(frame)
    assert viewport == Roi(x=80 / 640, y=40 / 360, width=480 / 640, height=280 / 360)
    assert model_box(frame) is None


def test_virtual_camera_can_extend_beyond_wide_source() -> None:
    from sculptcut.domain import CameraPoint, SubjectCamera
    from sculptcut.subject_camera import camera_preview

    frame = np.full((180, 640, 3), 70, dtype=np.uint8)
    frame[40:140, 200:440] = (30, 170, 255)
    camera = SubjectCamera(
        viewport=Roi(x=0, y=0, width=1, height=1),
        width=1.4,
        height=1.4 * 640 / 180 * 16 / 9,
        points=[CameraPoint(time_us=0, x=0.5, y=0.5)],
        background=70,
    )
    result = camera_preview(frame, camera, 0, 360, 640)
    assert result.shape == (640, 360, 3)
    assert tuple(result[0, 0]) == (70, 70, 70)
    assert result[320, 180, 2] > 200


def test_fitted_camera_leaves_room_for_subject_and_uses_video_background() -> None:
    from sculptcut.domain import CameraPoint, SubjectCamera
    from sculptcut.subject_camera import camera_filter, camera_preview

    frame = np.full((180, 320, 3), 35, dtype=np.uint8)
    frame[:, 160:] = 90
    frame[50:130, 135:185] = (20, 150, 255)
    camera = SubjectCamera(
        viewport=Roi(x=0, y=0, width=1, height=1),
        fit_subject=True,
        blur_background=True,
        width=0.5,
        height=1.6,
        points=[CameraPoint(time_us=0, x=0.5, y=0.5)],
        background=70,
    )
    result = camera_preview(frame, camera, 0, 360, 640)
    assert result[320, 180, 2] > 200
    assert 25 < int(result[0, 0, 0]) < 100
    assert int(result[0, 0, 0]) != camera.background
    assert "boxblur=30:2" in camera_filter(camera, 0, 1, 320, 180, 360, 640)


def test_native_portrait_fills_preview_and_export_without_reframing(tmp_path: Path) -> None:
    from sculptcut.media import discover, run_process
    from sculptcut.project import create_project
    from sculptcut.proxy import make_proxies
    from sculptcut.rendering import render, verify_output

    video = tmp_path / "portrait.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x403020:s=360x640:r=30:d=1",
            "-vf",
            "drawbox=x=0:y=0:w=36:h=640:color=red:t=fill,"
            "drawbox=x=324:y=0:w=36:h=640:color=green:t=fill",
            "-c:v",
            "libx264",
            str(video),
        ]
    )
    root = tmp_path / "project"
    project = create_project(root, [video]).model_copy(
        update={"settings": ProjectSettings(preset="shorts", proxy_width=360)}
    )
    make_proxies(root, project)
    base = build_plan(project, [], 1)
    plan = base.model_copy(
        update={
            "decisions": [
                RenderDecision(
                    source_id=project.sources[0].id,
                    source_start_us=0,
                    source_end_us=1_000_000,
                    action="SPEED",
                    output_frame_count=30,
                    provenance="test:native_portrait",
                )
            ],
            "actual_frame_count": 30,
            "target_frame_count": 30,
            "settings": base.settings
            | {"story": "process", "preset": "shorts", "width": 1080, "height": 1920},
        }
    )
    framed = reframe_shorts(root, project, plan, None)
    camera = framed.decisions[0].subject_camera
    assert camera is not None
    assert camera.viewport == Roi(x=0, y=0, width=1, height=1)
    assert (camera.width, camera.height) == (1, 1)
    assert not camera.fit_subject

    for preview in (True, False):
        output = render(
            root,
            project,
            framed,
            tmp_path / f"portrait-{preview}.mp4",
            preview=preview,
            encoder="libx264",
        )
        verify_output(output, 30, 30)
        capture = cv2.VideoCapture(str(output))
        try:
            ok, frame = capture.read()
            assert ok
            width = frame.shape[1]
            left = frame[frame.shape[0] // 2, width // 30]
            right = frame[frame.shape[0] // 2, width * 29 // 30]
            assert int(left[2]) > int(left[1]) * 2
            assert int(right[1]) > int(right[2]) * 2
        finally:
            capture.release()


def test_black_recording_is_replaced_without_changing_budget(tmp_path: Path) -> None:
    from sculptcut.media import discover, run_process
    from sculptcut.project import create_project
    from sculptcut.proxy import make_proxies
    from sculptcut.shorts import replace_black_shots

    video = tmp_path / "black-pause.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x120:r=5:d=180",
            "-vf",
            "drawbox=x=0:y=0:w=iw:h=ih:color=gray:t=fill:enable='gte(t,5)'",
            "-c:v",
            "libx264",
            str(video),
        ]
    )
    root = tmp_path / "project"
    project = create_project(root, [video]).model_copy(
        update={"settings": ProjectSettings(preset="shorts", proxy_width=160)}
    )
    make_proxies(root, project)
    plan = highlight_plan(project, build_plan(project, [], 60), [])
    replaced = replace_black_shots(root, project, plan, None)
    validate_plan(project, replaced)
    kept = [d for d in replaced.decisions if d.action != "DROP"]
    assert kept[0].source_start_us >= 5_000_000
    assert replaced.actual_frame_count == plan.actual_frame_count == 1800
    assert len(kept) == 20
    assert any("replaced black" in w for w in replaced.warnings)


@pytest.mark.parametrize("with_model", [True, False])
def test_vertical_render_and_preview_real_video(tmp_path: Path, with_model: bool) -> None:
    from sculptcut.media import discover, probe, run_process
    from sculptcut.project import create_project
    from sculptcut.proxy import make_proxies
    from sculptcut.rendering import render, verify_output

    video = tmp_path / "model.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x141414:s=640x360:r=30:d=3",
            "-vf",
            "drawbox=x=80:y=40:w=480:h=280:color=0x464646:t=fill"
            + (",drawbox=x=340:y=120:w=55:h=110:color=0xaaaaaa:t=fill" if with_model else ""),
            "-c:v",
            "libx264",
            str(video),
        ]
    )
    root = tmp_path / "project"
    project = create_project(root, [video]).model_copy(
        update={"settings": ProjectSettings(preset="shorts", proxy_width=640)}
    )
    make_proxies(root, project)
    plan = highlight_plan(project, build_plan(project, [], 60), [])
    plan = reframe_shorts(root, project, plan, None)
    assert isinstance(plan.decisions[0].crop, Roi)
    crop = plan.decisions[0].crop
    assert crop.x >= 79 / 640 and crop.y >= 39 / 360
    assert crop.x + crop.width <= 561 / 640
    assert crop.y + crop.height <= 321 / 360
    assert bool(plan.warnings[-1].startswith("Shorts viewport fallback")) is not with_model
    for preview in (False, True):
        output = render(
            root,
            project,
            plan,
            tmp_path / f"short-{preview}.mp4",
            preview=preview,
            encoder="libx264",
        )
        info = probe(output)
        assert (info.width, info.height) == ((360, 640) if preview else (1080, 1920))
        verify_output(output, 90, 30)


def test_unrecognized_application_is_letterboxed_instead_of_failing(tmp_path: Path) -> None:
    from sculptcut.media import discover, run_process
    from sculptcut.project import create_project
    from sculptcut.proxy import make_proxies
    from sculptcut.rendering import render, verify_output

    video = tmp_path / "mixed-app.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=640x360:r=30:d=3",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x141414:s=640x360:r=30:d=3",
            "-filter_complex",
            "[1:v]drawbox=x=80:y=40:w=480:h=280:color=0x464646:t=fill,"
            "drawbox=x=340:y=120:w=55:h=110:color=0xaaaaaa:t=fill[zbrush];"
            "[0:v][zbrush]concat=n=2:v=1:a=0[out]",
            "-map",
            "[out]",
            "-c:v",
            "libx264",
            str(video),
        ]
    )
    root = tmp_path / "project"
    project = create_project(root, [video]).model_copy(
        update={"settings": ProjectSettings(preset="shorts", proxy_width=640)}
    )
    make_proxies(root, project)
    plan = build_plan(project, [], 3).model_copy(
        update={
            "decisions": [
                RenderDecision(
                    source_id="source-0000",
                    source_start_us=0,
                    source_end_us=3_000_000,
                    action="SPEED",
                    output_frame_count=45,
                    provenance="test:unrecognized_application",
                ),
                RenderDecision(
                    source_id="source-0000",
                    source_start_us=3_000_000,
                    source_end_us=6_000_000,
                    action="SPEED",
                    output_frame_count=45,
                    provenance="test:zbrush",
                ),
            ],
            "settings": {"preset": "shorts", "width": 1080, "height": 1920},
        }
    )
    framed = reframe_shorts(root, project, plan, None)
    validate_plan(project, framed)
    first, second = framed.decisions
    assert first.crop == Roi(x=0, y=0, width=1, height=1)
    assert first.subject_camera is not None
    assert first.subject_camera.width == 1
    assert first.subject_camera.height > 3
    assert second.crop != first.crop
    assert second.subject_camera is not None
    assert any("unrecognized application" in warning for warning in framed.warnings)

    output = render(root, project, framed, tmp_path / "mixed-app-shorts.mp4", encoder="libx264")
    verify_output(output, 90, 30)


@pytest.mark.parametrize("vfr", [False, True])
def test_moving_subject_stays_visible_under_fixed_camera(tmp_path: Path, vfr: bool) -> None:
    from sculptcut.media import discover, run_process
    from sculptcut.project import create_project
    from sculptcut.proxy import make_proxies
    from sculptcut.rendering import render, verify_output

    video = tmp_path / "moving.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x141414:s=640x360:r=30:d=3",
            "-f",
            "lavfi",
            "-i",
            "color=c=0xffaa33:s=80x120:r=30:d=3",
            "-filter_complex",
            "[0:v]drawbox=x=80:y=40:w=480:h=280:color=0x464646:t=fill[bg];"
            "[bg][1:v]overlay=x='85+120*t':y='70+20*t':shortest=1"
            + (",setpts=3*PTS" if vfr else ""),
            "-fps_mode",
            "vfr",
            "-c:v",
            "libx264",
            str(video),
        ]
    )
    root = tmp_path / "project"
    project = create_project(root, [video]).model_copy(
        update={"settings": ProjectSettings(preset="shorts", proxy_width=640)}
    )
    make_proxies(root, project)
    plan = reframe_shorts(
        root, project, highlight_plan(project, build_plan(project, [], 60), []), None
    )
    camera = plan.decisions[0].subject_camera
    assert camera is not None and len(camera.points) == 1
    assert camera.fit_subject
    assert project.settings.shorts_subject_fill == 0.65
    assert plan.actual_frame_count == (267 if vfr else 90)
    for preview in (False, True):
        output = render(
            root,
            project,
            plan,
            tmp_path / f"tracked-{preview}.mp4",
            preview=preview,
            encoder="libx264",
        )
        verify_output(output, plan.actual_frame_count, 30)
        capture = cv2.VideoCapture(str(output))
        checked = 0
        centers = []
        try:
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                h, w = frame.shape[:2]
                mask = (frame[:, :, 2].astype(float) > frame[:, :, 1] * 1.3) & (frame[:, :, 1] > 90)
                ys, xs = np.where(mask)
                assert len(xs) > 100
                centers.append((xs.min() + xs.max()) / 2)
                assert ys.max() - ys.min() < h * 0.75
                assert xs.min() > w * 0.05 and xs.max() < w * 0.95
                assert ys.min() > h * 0.05 and ys.max() < h * 0.95
                checked += 1
        finally:
            capture.release()
        assert checked == plan.actual_frame_count
        assert max(centers) - min(centers) > w * 0.1


def test_process_episode_keeps_camera_fixed_and_model_visible(tmp_path: Path) -> None:
    from sculptcut.media import discover, run_process
    from sculptcut.project import create_project
    from sculptcut.proxy import make_proxies
    from sculptcut.rendering import render, verify_output
    from sculptcut.subject_camera import camera_filter, center_at

    video = tmp_path / "moving-process.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x141414:s=640x360:r=30:d=2",
            "-f",
            "lavfi",
            "-i",
            "color=c=0xffaa33:s=80x120:r=30:d=2",
            "-filter_complex",
            "[0:v]drawbox=x=80:y=40:w=480:h=280:color=0x464646:t=fill[bg];"
            "[bg][1:v]overlay=x='240+60*t':y=100:shortest=1",
            "-c:v",
            "libx264",
            str(video),
        ]
    )
    root = tmp_path / "project"
    project = create_project(root, [video]).model_copy(
        update={"settings": ProjectSettings(preset="shorts", proxy_width=640)}
    )
    make_proxies(root, project)
    base = highlight_plan(project, build_plan(project, [], 60), [])
    process = base.model_copy(update={"settings": base.settings | {"story": "process"}})
    plan = reframe_shorts(root, project, process, None)
    camera = plan.decisions[0].subject_camera
    assert camera is not None
    assert len(camera.points) == 1
    assert center_at(camera, 1_900_000) == center_at(camera, 0)
    assert camera.viewport.width * 640 > camera.viewport.height * 360
    assert camera.fit_subject and not camera.blur_background
    assert "clip((t-" not in camera_filter(camera, 0, 1, 640, 360, 360, 640)
    assert camera.width < camera.viewport.width
    assert camera.height <= camera.viewport.height
    output = render(root, project, plan, tmp_path / "tracked-process.mp4", encoder="libx264")
    verify_output(output, plan.actual_frame_count, plan.output_fps)
    reader = cv2.VideoCapture(str(output))
    try:
        centers = []
        while True:
            ok, frame = reader.read()
            if not ok:
                break
            mask = (frame[:, :, 2].astype(float) > frame[:, :, 1] * 1.3) & (frame[:, :, 1] > 90)
            ys, xs = np.where(mask)
            assert len(xs) > 100
            assert xs.min() > frame.shape[1] * 0.05
            assert xs.max() < frame.shape[1] * 0.95
            centers.append(float(xs.mean()))
    finally:
        reader.release()
    assert len(centers) == plan.actual_frame_count
    assert max(centers) - min(centers) > 10
    assert all(0.1 * 1080 < center < 0.9 * 1080 for center in centers)
