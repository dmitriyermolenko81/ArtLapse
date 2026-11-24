"""Whole-character continuity, chronological updates, erasure and safe reset."""

from pathlib import Path
from threading import Event

import cv2
import numpy as np
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton
from test_episode_panel import wait_until

from sculptcut.canvas_history import CanvasMemory, compose_history, enabled
from sculptcut.domain import ProjectSettings
from sculptcut.media import Cancelled, discover, run_process
from sculptcut.photoshop import reframe_photoshop
from sculptcut.photoshop_review_dialog import PhotoshopReviewDialog
from sculptcut.planning import build_plan, validate_plan
from sculptcut.project import create_project, load_project, save_project
from sculptcut.rendering import render, verify_output
from sculptcut.ui import MainWindow


def test_canvas_retains_unseen_parts_and_replaces_paint_and_erasures():
    memory = CanvasMemory()
    full = np.full((100, 100, 3), 210, np.uint8)
    full[10:30, 40:60] = (30, 50, 80)  # head
    full[70:90, 40:60] = (10, 100, 20)  # feet
    memory.update(full, (0, 0, 100, 100))
    head = np.full_like(full, 210)
    head[10:30, 40:60] = (0, 0, 240)
    result = memory.update(head, (30, 0, 70, 40))
    assert np.array_equal(result[15:25, 45:55], head[15:25, 45:55])
    assert np.array_equal(result[70:90, 40:60], full[70:90, 40:60])
    blank = np.full_like(full, 210)
    result = memory.update(blank, (20, 55, 80, 100))
    assert np.all(result[70:90, 40:60] == 210)  # actual erasure replaces old feet
    assert np.array_equal(result[15:25, 45:55], head[15:25, 45:55])
    memory.reset()
    result = memory.update(blank, (20, 55, 80, 100))
    assert np.all(result == 210)  # another pose must not inherit the old head
    memory.reset()
    border = blank.copy()
    border[:5] = (0, 0, 250)
    result = memory.update(border, (20, 20, 80, 80), (210, 210, 210))
    assert np.all(result[:5] == 210)  # excluded viewport chrome never seeds history


def test_control_persists_in_draft_template_project_and_undo(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.resize(1120, 700)
    window.show()
    try:
        window.preset.setCurrentIndex(window.preset.findData("photoshop_shorts"))
        app.processEvents()
        assert window.whole_model.isVisible() and not window.whole_model.isChecked()
        QTest.mouseClick(window.whole_model, Qt.MouseButton.LeftButton)
        assert window.whole_model.isChecked()
        assert window.form_draft()["photoshop_keep_model"]
        options = window.template_options()
        assert options.photoshop_keep_model
        window.history_controller.undo()
        assert not window.whole_model.isChecked()
        window.history_controller.redo()
        assert window.whole_model.isChecked()
        window.shorts_layout.setCurrentIndex(window.shorts_layout.findData("square"))
        assert not window.whole_model.isVisible()
        window.apply_template_options(options)
        assert window.whole_model.isChecked() and window.whole_model.isVisible()
        window.preset.setCurrentIndex(window.preset.findData("shorts"))
        assert not window.whole_model.isVisible()
        window.preset.setCurrentIndex(window.preset.findData("photoshop_music_shorts"))
        assert window.whole_model.isVisible()
    finally:
        window.close()
        app.processEvents()


def test_real_video_updates_head_then_feet_without_losing_the_rest(tmp_path: Path):
    image = np.full((360, 640, 3), 210, np.uint8)
    rng = np.random.default_rng(21)
    image[70:310, 270:370] = rng.integers(20, 100, (240, 100, 3), dtype=np.uint8)
    image[70:310, 270:370] = cv2.GaussianBlur(image[70:310, 270:370], (5, 5), 0.8)
    for _ in range(60):
        x, y = int(rng.integers(275, 365)), int(rng.integers(75, 305))
        cv2.circle(image, (x, y), int(rng.integers(2, 5)), (15, 15, 15), 1)
    for i, y in enumerate(range(130, 270, 25)):
        cv2.putText(image, f"{i}AB", (278, y), cv2.FONT_HERSHEY_SIMPLEX, .6,
                    (150, 150, 150), 2, cv2.LINE_AA)
    cv2.rectangle(image, (300, 80), (335, 110), (0, 200, 0), -1)
    cv2.rectangle(image, (300, 270), (335, 300), (0, 200, 0), -1)
    head = image.copy()
    cv2.rectangle(head, (300, 80), (335, 110), (0, 0, 245), -1)
    head = cv2.warpAffine(head, np.float64([[2, 0, -320], [0, 2, -60]]),
                          (640, 360), borderValue=(210, 210, 210))
    feet = image.copy()
    cv2.rectangle(feet, (300, 270), (335, 300), (245, 0, 0), -1)
    feet = cv2.warpAffine(feet, np.float64([[2, 0, -320], [0, 2, -300]]),
                          (640, 360), borderValue=(210, 210, 210))
    recovered = head.copy()
    cv2.rectangle(recovered, (280, 100), (350, 160), (0, 220, 240), -1)
    paths = []
    for index, frame in enumerate((image, head, feet, np.full_like(image, 210), recovered)):
        path = tmp_path / f"source-{index}.mp4"
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*"mp4v"), 10, (640, 360))
        assert writer.isOpened()
        for _ in range(30 if index == 3 else 10):
            writer.write(frame)
        writer.release()
        paths.append(path)
    root = tmp_path / "project"
    project = create_project(root, paths)
    project = project.model_copy(update={"settings": ProjectSettings(
        preset="photoshop_shorts", photoshop_keep_model=True, output_fps=10,
        target_seconds=7, final_view_seconds=0)})
    save_project(root, project)
    assert load_project(root).settings.photoshop_keep_model
    plan = reframe_photoshop(project, build_plan(project, [], 7), None)
    assert plan.settings["photoshop_registered_shots"] == 4
    assert all(d.subject_camera.canvas_registered for d in plan.decisions[:3])
    assert not plan.decisions[3].subject_camera.canvas_registered
    assert plan.decisions[4].subject_camera.canvas_registered
    off = project.model_copy(update={"settings": project.settings.model_copy(
        update={"photoshop_keep_model": False})})
    assert build_plan(off, [], 7).settings["photoshop_keep_model"] == 0
    with pytest.raises(ValueError, match="Canvas memory changed"):
        validate_plan(off, plan)
    assert not enabled(project.settings.model_copy(update={"shorts_layout": "square"}))
    output = render(root, project, plan, root / "renders" / "whole.mp4", encoder="libx264")
    verify_output(output, 70, 10)
    run_process([discover('ffmpeg'), '-v', 'error', '-xerror', '-i', str(output),
                 '-f', 'null', '-'])
    capture = cv2.VideoCapture(str(output))
    frames = []
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        frames.append(frame)
    capture.release()
    assert len(frames) == 70

    def areas(frame):
        b, g, r = cv2.split(frame)
        green = (g > 130) & (r < 65) & (b < 65)
        red = (r > 160) & (g < 65) & (b < 65)
        blue = (b > 160) & (g < 65) & (r < 65)
        return [int(np.count_nonzero(mask)) for mask in (green, red, blue)]

    initial, head_updated, feet_updated = [areas(frames[i]) for i in (5, 15, 25)]
    assert initial[0] > 1000 and initial[1] < 20 and initial[2] < 20
    assert head_updated[0] > initial[0]*.35 and head_updated[1] > initial[0]*.35
    assert feet_updated[0] < 30 and feet_updated[1] > initial[0]*.35
    assert feet_updated[2] > initial[0]*.35

    assert areas(frames[35])[1] > 1000  # a brief gap holds the complete model
    assert areas(frames[59])[1] > 1000 and areas(frames[59])[2] > 1000

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    window.folder.setText(str(root))
    window.open_video(output, 'Photoshop Shorts')
    try:
        assert window.review_button.isVisible() and window.review_button.isEnabled()
        QTest.mouseClick(window.review_button, Qt.MouseButton.LeftButton)
        dialog = window.findChild(PhotoshopReviewDialog)
        assert dialog is not None and dialog.isVisible()
        row, marker = next((i, m) for i, m in enumerate(dialog.markers)
                           if m.decision.source_id == project.sources[3].id)
        QTest.mouseClick(dialog.timeline, Qt.MouseButton.LeftButton,
                         pos=QPoint(dialog.timeline.x_at(marker.start), 10))
        assert dialog.choices.currentIndex() == row
        assert dialog.frame_slider.value() == 30
        wait_until(app, lambda: not dialog.montage.frame.isNull()
                   and not dialog.original.frame.isNull())
        assert any(key[:2] == (output.resolve(), 3_000_000)
                   for key in dialog.montage.images.cache)
        assert any(key[:2] == (paths[3].resolve(), 0) for key in dialog.original.images.cache)
        dialog.frame_slider.setValue(35)
        wait_until(app, lambda: any(key[1] == 500_000 for key in dialog.original.images.cache))
        wait_until(app, lambda: any(key[1] == 3_500_000 for key in dialog.montage.images.cache))
        edit = next(button for button in dialog.findChildren(QPushButton)
                    if button.text() == 'Открыть эпизод в редакторе')
        QTest.mouseClick(edit, Qt.MouseButton.LeftButton)
        wait_until(app, lambda: window.selection_dialog is not None
                   and not window.selection_dialog.loading)
        editor = window.selection_dialog
        assert editor.isVisible() and editor.montage_seek.value() == 3500
        assert editor.sources.currentData() == project.sources[3].id
        assert not editor.session.changed
    finally:
        if (dialog := window.findChild(PhotoshopReviewDialog)) is not None:
            dialog.reject()
        window._allow_close = True
        window.close()
        app.processEvents()
    assert areas(frames[65])[2] > 1000  # feet survive recovery of the head update
    b, g, r = cv2.split(frames[65])
    assert np.count_nonzero((r > 160) & (g > 160) & (b < 65)) > 1000

    off_plan = plan.model_copy(update={"settings": plan.settings | {"photoshop_keep_model": 0}})
    plain = render(root, off, off_plan, tmp_path / "ordinary.mp4", encoder="libx264")
    capture = cv2.VideoCapture(str(plain))
    capture.set(cv2.CAP_PROP_POS_FRAMES, 15)
    ok, frame = capture.read()
    capture.release()
    assert ok and areas(frame)[0] < 30  # unchecked: the cropped feet stay absent

    interrupted = Event()
    partial = tmp_path / "interrupted.mkv"
    with pytest.raises(Cancelled):
        compose_history([(output, plan.decisions[0])], {s.id: s for s in project.sources},
                        partial, 10, 1080, 1920, interrupted, lambda _: interrupted.set())
    assert not partial.exists() and output.is_file()

    cancelled = Event()
    cancelled.set()
    with pytest.raises(Cancelled):
        compose_history([], {}, tmp_path / "cancel.mkv", 10, 1080, 1920, cancelled)
    assert not (tmp_path / "cancel.mkv").exists()


def test_patch_alignment_removes_jitter_without_freezing_new_paint():
    rng = np.random.default_rng(91)
    image = np.full((640, 360, 3), 210, np.uint8)
    image[150:580, 110:250] = rng.integers(20, 110, (430, 140, 3), dtype=np.uint8)
    cv2.rectangle(image, (165, 540), (190, 565), (0, 230, 0), -1)
    memory = CanvasMemory()
    memory.update(image, (0, 0, 360, 640))
    centers = []
    for i, shift in enumerate((4, -3, 3, -4)):
        current = image.copy()
        cv2.rectangle(current, (145, 300), (165+i*5, 320), (0, 0, 240), -1)
        moved = cv2.warpAffine(current, np.array([[1, 0, shift], [0, 1, 0]],
                                              dtype=np.float64), (360, 640),
                              borderValue=(210, 210, 210))
        result = memory.update(moved, (10, 140, 350, 600))
        green = (result[:, :, 1] > 170) & (result[:, :, 2] < 50)
        centers.append(np.where(green)[1].mean())
        assert result[310, 148+i*5, 2] > 180
    assert np.ptp(centers) < 1.5


def test_patch_alignment_locks_small_zoom_and_larger_pan_with_live_paint():
    rng = np.random.default_rng(103)
    image = np.full((640, 360, 3), 210, np.uint8)
    image[140:580, 60:300] = rng.integers(30, 130, (440, 240, 3), dtype=np.uint8)
    cv2.rectangle(image, (100, 480), (124, 504), (0, 230, 0), -1)
    memory = CanvasMemory()
    memory.update(image, (0, 0, 360, 640))
    positions = []
    widths = []
    for i, (zoom, dx) in enumerate([(1.009, 10), (.993, -9), (1.006, 14), (1, -12)]):
        current = image.copy()
        cv2.rectangle(current, (175, 300), (195+i*5, 320), (0, 0, 240), -1)
        matrix = cv2.getRotationMatrix2D((180, 340), 0, zoom)
        matrix[:, 2] += (dx, -3)
        moved = cv2.warpAffine(current, matrix, (360, 640), borderValue=(210, 210, 210))
        result = memory.update(moved, (15, 100, 345, 610))
        green = (result[:, :, 1] > 180) & (result[:, :, 2] < 50)
        yy, xx = np.where(green)
        positions.append((xx.mean(), yy.mean()))
        widths.append(np.ptp(xx))
        assert result[310, 181+i*5, 2] > 180
    assert np.max(np.ptp(positions, axis=0)) < 1.5
    assert np.ptp(widths) <= 2


def test_patch_alignment_rejects_large_zoom_and_local_moving_detail():
    from sculptcut.canvas_history import align_patch

    rng = np.random.default_rng(104)
    image = rng.integers(40, 200, (640, 360, 3), dtype=np.uint8)
    zoom = cv2.getRotationMatrix2D((180, 320), 0, 1.1)
    moved = cv2.warpAffine(image, zoom, (360, 640))
    bounds = (0, 0, 360, 640)
    known = np.ones((640, 360), dtype=np.bool_)
    result, actual = align_patch(moved, image, known, bounds)
    assert result is moved and actual == bounds

    # Many corners in one moving selection handle cannot register the drawing.
    image[:] = 210
    image[310:330, 170:190] = rng.integers(0, 160, (20, 20, 3), dtype=np.uint8)
    moved = cv2.warpAffine(image, np.float64([[1, 0, 6], [0, 1, 0]]), (360, 640),
                           borderValue=(210, 210, 210))
    result, actual = align_patch(moved, image, known, bounds)
    assert result is moved and actual == bounds


def test_canvas_immutable_references_prevent_cumulative_scale_drift(monkeypatch):
    from dataclasses import replace

    from sculptcut.canvas_history import SETTINGS

    monkeypatch.setattr('sculptcut.canvas_history.SETTINGS', replace(
        SETTINGS, alignment_reference_interval=10))
    rng = np.random.default_rng(105)
    image = np.full((320, 180, 3), 210, np.uint8)
    image[60:280, 35:150] = rng.integers(30, 130, (220, 115, 3), dtype=np.uint8)
    cv2.rectangle(image, (60, 240), (80, 260), (0, 230, 0), -1)
    memory = CanvasMemory()
    memory.update(image, (0, 0, 180, 320))
    centers = []
    for i in range(70):
        current = image.copy()
        cv2.rectangle(current, (100, 140), (110+i//10, 155), (0, 0, 240), -1)
        matrix = cv2.getRotationMatrix2D((90, 170), 0, 1.008 if i % 2 else .994)
        matrix[:, 2] += (3 if i % 3 else -3, .3)
        moved = cv2.warpAffine(current, matrix, (180, 320), borderValue=(210, 210, 210))
        result = memory.update(moved, (8, 45, 172, 295))
        green = (result[:, :, 1] > 180) & (result[:, :, 2] < 50)
        yy, xx = np.where(green)
        centers.append((xx.mean(), yy.mean(), np.ptp(xx)))
    assert np.max(np.ptp(centers, axis=0)) < 1.5
    assert result[147, 115, 2] > 180
    assert len(memory.references) == 4
    memory.reset()
    assert not memory.references


def test_character_camera_keeps_legacy_translation_without_page_scale_anchors():
    from sculptcut.canvas_history import _patch_transform

    rng = np.random.default_rng(106)
    frame = np.full((640,360,3),210,np.uint8)
    frame[140:580,90:270] = rng.integers(30,130,(440,180,3),dtype=np.uint8)
    memory = CanvasMemory(refined=False)
    memory.update(frame,(0,0,360,640))
    moved = cv2.warpAffine(frame,np.float64([[1,0,4],[0,1,0]]),(360,640),
                           borderValue=(210,210,210))
    matrix = _patch_transform(moved,frame,np.ones((640,360),dtype=np.bool_),
                              (10,100,350,610),refined=False)
    assert matrix is not None and matrix[0,0] == matrix[1,1] == 1
    memory.update(moved,(10,100,350,610))
    assert not memory.references


def test_character_residual_alignment_preserves_scale_after_reference_fit():
    from sculptcut.canvas_history import _patch_transform

    rng = np.random.default_rng(365)
    frame = np.full((640,360,3),210,np.uint8)
    frame[100:590,90:270] = rng.integers(30,140,(490,180,3),dtype=np.uint8)
    matrix = cv2.getRotationMatrix2D((180,340),0,1.008)
    moved = cv2.warpAffine(frame,matrix,(360,640),borderValue=(210,210,210))
    correction = _patch_transform(moved,frame,np.ones((640,360),dtype=np.bool_),
                                   (5,5,355,635),refined=False)
    assert correction is not None
    assert np.array_equal(correction[:,:2],np.eye(2))
    assert correction[0,2] == correction[1,2] == 0


def test_character_guide_cleanup_keeps_unseen_head_and_neutral_sketch():
    memory = CanvasMemory(refined=False)
    frame = np.full((200,120,3),(205,175,160),np.uint8)
    frame[12:14,20:100] = (192,162,147)
    frame[25:45,50:70] = (20,35,50)
    memory.update(frame,(0,0,120,200))
    current = np.full_like(frame,(205,175,160))
    result = memory.update(current,(0,60,120,190))
    assert np.max(np.abs(result[12:14,20:100].astype(float)-(205,175,160))) <= 3
    assert np.all(result[60:190] == (205,175,160))
    assert np.all(result[25:45,50:70] == (20,35,50))
    memory.reset()
    neutral = np.full_like(frame,250)
    neutral[25:45,50:70] = 235
    memory.update(neutral,(0,0,120,200))
    result = memory.update(neutral,(0,60,120,190))
    assert np.all(result[25:45,50:70] == 235)
    memory.reset()
    coloured = np.full_like(frame,(210,180,165))
    memory.update(coloured,(0,0,120,200))
    current = np.full_like(frame,(205,175,160))
    result = memory.update(current,(0,60,120,190))
    assert np.all(result[:60] == result[60])  # continuous with the feathered visible edge
