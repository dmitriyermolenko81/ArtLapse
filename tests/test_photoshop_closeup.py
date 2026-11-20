"""Exercise automatic close-ups on the output clock and in a real export."""

from pathlib import Path

import cv2
import numpy as np

from sculptcut.canvas_history import CanvasMemory, update_closeup, visible_bounds
from sculptcut.domain import CameraPoint, ProjectSettings, Roi, SubjectCamera
from sculptcut.drawing_reference import DrawingReference
from sculptcut.photoshop_closeup import (
    CloseupSample,
    closeup_camera,
    long_gaps,
    red_mask_candidate,
    reflected_view,
    transient_masks,
)
from sculptcut.planning import RenderDecision, build_plan
from sculptcut.project import create_project
from sculptcut.rendering import render, verify_output
from sculptcut.subject_camera import camera_preview


def test_long_gaps_ignore_drops_and_keep_deliberate_holds():
    def decision(count, action="SPEED"):
        return RenderDecision(source_id="s", source_start_us=0, source_end_us=3000000,
                              action=action, output_frame_count=count, provenance="test")
    decisions = [decision(20), decision(0, "DROP"), decision(10), decision(6, "HOLD"),
                 decision(29)]
    assert list(long_gaps(decisions, 10)) == [[0, 2]]


def test_red_mask_requires_a_bounded_colour_change():
    frame = np.full((120, 300, 3), (20, 25, 135), dtype=np.uint8)
    bounds = (0, 0, 300, 120)
    assert red_mask_candidate(frame, bounds)
    frame[:, :45] = (75, 54, 39)
    assert not red_mask_candidate(frame, bounds)
    assert list(transient_masks([(0, 0, True), (0, 1, True)], 10)) == []
    assert list(transient_masks([(0, 0, False), (0, 1, True), (1, 0, True),
                                 (1, 1, False)], 10)) == [(0, 1), (1, 0)]


def test_document_edge_guard_scales_with_source_zoom():
    view = Roi(x=.3, y=.2, width=.4, height=.6)
    camera = SubjectCamera(
        viewport=view, width=.4, height=1.2, fit_subject=True, background=40,
        points=[CameraPoint(time_us=0, x=.5, y=.5, visible_region=view)],
    )
    old = visible_bounds(camera, 0, 1920, 1080, 1080, 1920)
    guarded = visible_bounds(camera, 0, 1920, 1080, 1080, 1920, source_inset_fraction=.010)
    assert guarded[0] > old[0] and guarded[1] > old[1]
    assert guarded[2] < old[2] and guarded[3] < old[3]


def test_closeup_shows_current_paint_without_seeding_shared_canvas(tmp_path: Path):
    fps = 6
    whole = np.full((180, 320, 3), 214, np.uint8)
    whole[110:150, 145:175] = (0, 215, 0)
    close = np.full_like(whole, (180, 0, 200))
    close[15:165, 15:305] = (110, 78, 54)
    close[58:130, 143:176] = (0, 0, 230)
    update = close.copy()
    update[58:130, 143:176] = (0, 210, 240)
    uncovered = np.full_like(whole, (240, 0, 230))
    paths = []
    for i, images in enumerate([[whole]*fps, [close]*(fps*2)+[update]*(fps*2),
                                [uncovered]*fps]):
        path = tmp_path / f"source-{i}.mp4"
        video = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*"mp4v"),
                                fps, (320, 180))
        assert video.isOpened()
        for image in images:
            video.write(image)
        video.release()
        paths.append(path)
    root = tmp_path / "project"
    project = create_project(root, paths).model_copy(update={"settings": ProjectSettings(
        preset="photoshop_shorts", photoshop_keep_model=True, output_fps=fps,
        target_seconds=6, final_view_seconds=0,
    )})
    plan = build_plan(project, [], 6)
    d = plan.decisions[0]
    view = Roi(x=.05, y=.09, width=.9, height=.82)
    full = Roi(x=0, y=0, width=1, height=1)
    shared = SubjectCamera(
        viewport=full, canvas_registered=True, fit_subject=True, width=.31, height=.98,
        points=[CameraPoint(time_us=0, x=.5, y=.5, reference_matched=True, visible_region=full)],
        background=214,
    )
    decisions = list(plan.decisions)
    decisions[0] = d.model_copy(update={"subject_camera": shared})
    number = decisions[1].output_frame_count
    camera = closeup_camera(
        [decisions[1].source_end_us*i//number for i in range(number)],
        [Roi(x=.43, y=.3, width=.15, height=.45)]*number, [view]*number, 320, 180,
    )
    assert camera is not None and camera.frame_only
    camera = camera.model_copy(update={
        "closeup_group": 1,
        "points": [p.model_copy(update={"reference_matched": True}) for p in camera.points],
    })
    decisions[1] = decisions[1].model_copy(update={"subject_camera": camera})
    assert decisions[2].subject_camera is None
    restored = plan.model_copy(update={"decisions": decisions})
    path = render(root, project, restored, tmp_path / "closeup.mp4", encoder="libx264")
    verify_output(path, fps*6, fps)
    cap = cv2.VideoCapture(str(path))
    images = []
    try:
        for at in (2, fps+3, 3*fps+3, fps*5+3):
            cap.set(cv2.CAP_PROP_POS_FRAMES, at)
            ok, image = cap.read()
            assert ok
            images.append(image)
    finally:
        cap.release()

    def pixel_counts(image):
        b, g, r = cv2.split(image)
        green = (g > 140) & (b < 80) & (r < 80)
        red = (r > 150) & (g < 70) & (b < 70)
        yellow = (r > 150) & (g > 150) & (b < 70)
        chrome = (b > 140) & (r > 140) & (g < 70)
        return [np.count_nonzero(mask) for mask in (green, red, yellow, chrome)]

    first, detail, painted, held = map(pixel_counts, images)
    # H.264 may shift a few edge pixels across the colour threshold.
    assert first[0] > 1000 and abs(held[0]-first[0]) < first[0]*.001
    assert detail[1] > 1000 and painted[2] > 1000
    assert max(detail[0], painted[0], held[1], held[2]) < 20
    assert max(first[3], detail[3], painted[3], held[3]) < 20


def test_closeup_aligns_pan_and_mirror_and_copies_only_verified_paint():
    w, h = 360, 320
    image = np.full((h, w, 3), (94, 64, 45), np.uint8)
    rng = np.random.default_rng(12)
    for x, y, size in rng.integers((100, 65, 4), (250, 255, 12), size=(65, 3)):
        cv2.rectangle(image, (x, y), (x+size, y+size), (140, 200, 230), -1)
    cv2.circle(image, (102, 125), 10, (240, 50, 40), -1)
    view = Roi(x=.08, y=.05, width=.82, height=.9)
    seed = SubjectCamera(
        viewport=view, fit_subject=True, frame_only=True, closeup_group=1,
        points=[CameraPoint(time_us=0, x=.5, y=.5)], width=.5, height=1,
        background=40,
    )
    reference = DrawingReference(image, view, seed)
    painted = image.copy()
    cv2.circle(painted, (194, 156), 8, (30, 220, 25), -1)
    moved = cv2.warpAffine(painted, np.float64([[1, 0, -17], [0, 1, 9]]), (w, h))
    moved = cv2.flip(moved, 1)
    # A magenta strip outside the verified document must never enter memory.
    moved[:, :145] = (255, 0, 255)
    partial = reflected_view(Roi(x=.08, y=.08, width=.51, height=.85))

    memory = CanvasMemory()
    for t, frame, document in ((0, image, view), (1, moved, partial)):
        point, _ = CloseupSample.from_frame(frame, document, reference).match(reference, t)
        assert point is not None and point.flip_horizontal == bool(t)
        assert abs(point.x*w - (180 if t == 0 else w-163)) < 2
        assert abs(point.y*h - (160 if t == 0 else 169)) < 2
        point = point.model_copy(update={"reference_matched": True})
        camera = seed.model_copy(update={"viewport": document, "points": [point]})
        decoded = camera_preview(frame, camera, t, 180, 320)
        out = update_closeup(memory, decoded, camera, t, w, h)
        assert out is not None

    assert out[124, 12, 0] > 180  # Unchanged blue reference detail.
    assert out[156, 104, 1] > 150  # Newly painted green in the reference coordinates.
    assert not np.any((out[:, :, 0] > 200) & (out[:, :, 2] > 200))
    rejected = camera.model_copy(update={"points": [point.model_copy(
        update={"reference_matched": False, "visible_region": None})]})
    assert update_closeup(memory, np.full_like(out, 255), rejected, 1, w, h) is out
