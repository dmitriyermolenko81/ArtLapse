"""One readable framing through source pans, hard cuts and source zooms."""

import json

import cv2
import numpy as np
import pytest

from sculptcut.domain import CameraPoint, ProjectSettings, Roi, SubjectCamera
from sculptcut.drawing_reference import DrawingReference, locked_reference_points
from sculptcut.photoshop import reframe_photoshop, shared_scene_scales
from sculptcut.planning import RenderDecision, build_plan, validate_plan
from sculptcut.project import create_project, save_project
from sculptcut.rendering import render, verify_output
from sculptcut.subject_camera import camera_preview


def test_similar_scenes_share_scale_without_merging_large_source_zoom():
    cams = [
        SubjectCamera(
            viewport=Roi(x=0, y=0, width=1, height=1),
            fit_subject=True,
            width=h * 0.3,
            height=h,
            background=32,
            points=[CameraPoint(time_us=0, x=x, y=0.5, width=h * 0.3, height=h)],
        )
        for h, x in [(1, 0.2), (0.98, 0.8), (0.5, 0.4)]
    ]
    fitted = shared_scene_scales(cams)
    assert fitted[0].height == fitted[1].height == 1
    assert fitted[2].height == 0.5
    assert [c.points[0].x for c in fitted] == [0.2, 0.8, 0.4]


def test_reference_matches_artwork_instead_of_stationary_photoshop_chrome():
    rng = np.random.default_rng(4)
    texture = rng.integers(0, 255, (120, 80, 3), dtype=np.uint8)
    before = np.full((320, 480, 3), 210, np.uint8)
    before[100:220, 200:280] = texture
    after = np.full_like(before, 210)
    after[95:245, 230:330] = cv2.resize(texture, (100, 150))
    for image in (before, after):
        for y in range(12, 300, 15):
            cv2.putText(image, "Tools 123", (0, y), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (0, 0, 0))
    viewport = Roi(x=0.2, y=0.15, width=0.7, height=0.7)
    camera = SubjectCamera(
        viewport=viewport,
        fit_subject=True,
        width=0.25,
        height=0.6,
        background=32,
        points=[CameraPoint(time_us=0, x=0.5, y=0.5)],
    )
    ref = DrawingReference(before, viewport, camera)
    point = ref.point(after, viewport, 100_000)
    assert ref.point(after, viewport, 100_000, ref.features(after, viewport)) == point
    assert point is not None
    assert abs(point.x - 280 / 480) < 0.005 and abs(point.y - 170 / 320) < 0.005
    assert abs(point.height / 0.6 - 1.25) < 0.02
    assert ref.point(np.full_like(before, 210), viewport, 200_000) is None
    tiny = viewport.model_copy(update={"width": 0.001, "height": 0.001})
    assert DrawingReference(before, tiny, camera).point(after, tiny, 0) is None


def test_reference_keeps_full_drawing_center_outside_a_zoomed_detail():
    rng = np.random.default_rng(4)
    before = np.full((320, 480, 3), 210, np.uint8)
    before[80:220, 150:300] = rng.integers(0, 255, (140, 150, 3), dtype=np.uint8)
    after = cv2.warpAffine(
        before, np.float64([[3, 0, -480], [0, 3, -120]]), (480, 320), borderValue=(210, 210, 210)
    )
    viewport = Roi(x=0, y=0, width=1, height=1)
    camera = SubjectCamera(
        viewport=viewport,
        fit_subject=True,
        width=0.25,
        height=0.6,
        background=32,
        points=[CameraPoint(time_us=0, x=0.5, y=0.5)],
    )
    p = DrawingReference(before, viewport, camera).point(after, viewport, 0)
    assert p is not None
    assert abs(p.x - 0.5) < 0.01 and abs(p.y - 1.125) < 0.01
    assert abs(p.height - 1.8) < 0.03
    assert CameraPoint.model_validate_json(p.model_dump_json()) == p


def test_registration_gaps_do_not_create_a_pan_or_zoom_animation():
    a = CameraPoint(time_us=0, x=0.5, y=0.5, width=0.3, height=0.8)
    b = CameraPoint(time_us=40, x=0.9, y=1.5, width=0.9, height=2.4)
    points = locked_reference_points([a, None, None, None, b], [0, 10, 20, 30, 40])
    assert {p.width for p in points} == {0.3, 0.9}
    assert {p.y for p in points} == {0.5, 1.5}
    jitter = [a.model_copy(update={"time_us": i, "x": 0.5 + i * 0.0001}) for i in range(5)]
    frozen = locked_reference_points(jitter, list(range(5)))
    assert len({(p.x, p.y, p.width, p.height) for p in frozen}) == 1


def test_one_framing_across_sources_with_offscreen_body_center(tmp_path):
    image = np.full((360, 640, 3), 210, np.uint8)
    rng = np.random.default_rng(8)
    image[80:300, 270:370] = rng.integers(0, 100, (220, 100, 3), dtype=np.uint8)
    cv2.rectangle(image, (310, 104), (329, 123), (0, 250, 0), -1)
    zoomed = cv2.warpAffine(
        image, np.float64([[3, 0, -640], [0, 3, -200]]), (640, 360), borderValue=(210, 210, 210)
    )
    paths = []
    for n, frame in enumerate((image, zoomed)):
        path = tmp_path / f"source{n}.mp4"
        paths.append(path)
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 15, (640, 360))
        assert writer.isOpened()
        for _ in range(30):
            writer.write(frame)
        writer.release()
    root = tmp_path / "project"
    p = create_project(root, paths).model_copy(
        update={
            "settings": ProjectSettings(
                preset="photoshop_shorts", output_fps=15, target_seconds=4, final_view_seconds=0,
                photoshop_keep_model=True,
            )
        }
    )
    save_project(root, p)
    plan = reframe_photoshop(p, build_plan(p, [], 4), None)
    assert plan.settings["photoshop_registered_shots"] == 2
    assert plan.settings["photoshop_reference_source"] == p.sources[0].id
    assert any(q.y > 1 for q in plan.decisions[1].subject_camera.points)
    out = render(root, p, plan, tmp_path / "fixed.mp4", encoder="libx264")
    verify_output(out, 60, 15)
    c = cv2.VideoCapture(str(out))
    centres = []
    try:
        while True:
            ok, frame = c.read()
            if not ok:
                break
            ys, xs = np.where(
                (frame[:, :, 1] > 180) & (frame[:, :, 0] < 80) & (frame[:, :, 2] < 80)
            )
            assert xs.size
            centres.append(((xs.min() + xs.max()) / 2, (ys.min() + ys.max()) / 2))
    finally:
        c.release()
    assert np.max(np.ptp(centres, axis=0)) < 6


@pytest.mark.parametrize("preset", ["photoshop_shorts", "photoshop_music_shorts"])
def test_render_keeps_subject_fixed_across_episode_cuts_and_source_zoom(tmp_path, preset):
    source = tmp_path / "pan-cut-zoom.mp4"
    writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*"mp4v"), 15, (320, 180))
    assert writer.isOpened()
    originals = []
    texture = np.full((96, 64, 3), (0, 0, 240), np.uint8)
    random = np.random.default_rng(10)
    for x, y, radius in random.integers([5, 5, 1], [59, 91, 4], size=(60, 3)):
        cv2.circle(texture, (int(x), int(y)), int(radius), (10, 10, 50), -1)
    for i in range(90):
        shot, frame = divmod(i, 30)
        image = np.full((180, 320, 3), (210, 180, 150), np.uint8)
        w, h, x, y = [(40, 60, 25, 55), (41, 61, 185, 30), (64, 96, 70, 35)][shot]
        x += round(frame * 1.2)
        image[y : y + h, x : x + w] = cv2.resize(texture, (w, h))
        writer.write(image)
        originals.append(image)
    writer.release()
    root = tmp_path / "project"
    project = create_project(root, [source]).model_copy(
        update={
            "settings": ProjectSettings(
                preset=preset, output_fps=15, target_seconds=6, final_view_seconds=0
            )
        }
    )
    save_project(root, project)
    sid = project.sources[0].id
    base = build_plan(project, [], 6)
    base = base.model_copy(
        update={
            "decisions": [
                RenderDecision(
                    source_id=sid,
                    source_start_us=i * 2_000_000,
                    source_end_us=(i + 1) * 2_000_000,
                    action="SPEED",
                    output_frame_count=30,
                    provenance="test:episode",
                )
                for i in range(3)
            ],
            "settings": base.settings | {"width": 360, "height": 640},
        }
    )
    plan = reframe_photoshop(project, base, None)
    validate_plan(project, plan)
    assert plan.settings["photoshop_camera"] == (
        "local-episode-366" if preset == "photoshop_shorts" else "fixed-reference-352")
    assert plan.settings["photoshop_registered_shots"] == 3
    output = render(root, project, plan, tmp_path / "settled.mp4", encoder="libx264")
    verify_output(output, 90, 15)
    reader = cv2.VideoCapture(str(output))
    measured = []
    try:
        for i in range(90):
            ok, frame = reader.read()
            assert ok
            camera = plan.decisions[i // 30].subject_camera
            preview = camera_preview(originals[i], camera, round(i * 1e6 / 15), 360, 640)
            for image in (frame, preview):
                ys, xs = np.where((image[:, :, 2] > 180) & (image[:, :, 0] < 80))
                assert xs.size
                ih, iw = image.shape[:2]
                cx, cy = (xs.min() + xs.max()) / 2 / iw, (ys.min() + ys.max()) / 2 / ih
                assert abs(cx - 0.5) < 0.025 and abs(cy - 0.5) < 0.025
            measured.append([float(cx), float(cy), float((xs.max() - xs.min()) / iw)])
    finally:
        reader.release()
    # One framing for the montage, including the first frame after each hard cut.
    widths = [row[2] for row in measured]
    assert max(widths) - min(widths) < 0.055
    (tmp_path / "measurements.json").write_text(json.dumps(measured))
