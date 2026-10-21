"""Webcam motion must not enter analysis or modify exported pixels."""

from pathlib import Path
from time import monotonic

import cv2
import numpy as np
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from sculptcut.analysis import analysis_key, extract_features, measure, read_features
from sculptcut.crop_dialog import CropCanvas, WebcamDialog
from sculptcut.domain import Roi
from sculptcut.media import discover, run_process
from sculptcut.planning import simple_speed_plan
from sculptcut.project import create_project, load_project, save_project
from sculptcut.proxy import proxy_key
from sculptcut.rendering import render, verify_output
from sculptcut.ui import MainWindow
from sculptcut.webcam import without_webcam

ROI = Roi(x=0, y=0.6, width=0.3, height=0.4)


def test_webcam_changes_are_absent_from_all_frame_features() -> None:
    rng = np.random.default_rng(7)
    before = rng.integers(20, 200, (100, 160, 3), dtype=np.uint8)
    after = before.copy()
    after[60:, :48] = 255
    before_copy = before.copy()
    a, b = without_webcam(before, ROI), without_webcam(after, ROI)
    assert np.array_equal(a, b)
    assert np.array_equal(before, before_copy)
    assert without_webcam(before, None) is before
    gray = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
    result = measure(cv2.cvtColor(b, cv2.COLOR_BGR2GRAY), gray,
                     Roi(x=0, y=0, width=1, height=1), "source", 1)
    assert result.roi_change == result.ui_change == result.histogram_change == 0
    after[25:35, 70:80] = 255
    assert not np.array_equal(a, without_webcam(after, ROI))
    with pytest.raises(ValueError, match="leave part"):
        without_webcam(before, Roi(x=0, y=0, width=1, height=1))


def make_video(path: Path) -> None:
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 "color=c=gray:s=160x100:r=5:d=3", "-f", "lavfi", "-i",
                 "testsrc2=s=48x40:r=5:d=3", "-filter_complex", "[0][1]overlay=0:60",
                 "-c:v", "libx264", "-crf", "0", str(path)])


def test_real_analysis_recomputes_without_modifying_proxy(tmp_path: Path) -> None:
    source = tmp_path / "camera.mp4"
    make_video(source)
    root = tmp_path / "project"
    project = create_project(root, [source])
    source_info = project.sources[0]
    project = project.model_copy(update={"settings": project.settings.model_copy(update={
        "proxy_width": 160, "proxy_fps": 5,
    })})
    extract_features(root, project)
    original = list(read_features(root, project, source_info))
    assert any(f.roi_change > 0.0001 for f in original)
    masked = project.model_copy(update={"settings": project.settings.model_copy(update={
        "webcam_enabled": True, "webcam_rois": {source_info.id: ROI},
    })})
    assert analysis_key(masked, source_info) != analysis_key(project, source_info)
    assert proxy_key(masked, source_info) == proxy_key(project, source_info)
    extract_features(root, masked)
    filtered = list(read_features(root, masked, source_info))
    assert len(filtered) == len(original)
    assert max(f.roi_change for f in filtered) < 0.00005
    save_project(root, masked)
    loaded = load_project(root)
    assert loaded.settings.webcam_rois[source_info.id] == ROI
    assert loaded.settings.webcam_enabled
    output = tmp_path / "result.mp4"
    render(root, masked, simple_speed_plan(masked, 1), output, encoder="libx264")
    verify_output(output, 30, 30)
    run_process([discover("ffmpeg"), "-v", "error", "-xerror", "-i", str(output),
                 "-f", "null", "-"])
    capture = cv2.VideoCapture(str(output))
    try:
        ok, frame = capture.read()
        assert ok
        # The moving coloured webcam patch must still be visible in the export.
        assert np.std(frame[65:95, 5:43]) > 20
    finally:
        capture.release()


def test_free_rectangle_and_project_restoration(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    image = QPixmap(400, 200)
    image.fill(Qt.GlobalColor.gray)
    canvas = CropCanvas(image, ROI, 0)
    window = MainWindow()
    try:
        canvas.show()
        window.show()
        app.processEvents()
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(120, 199))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(180, 180))
        assert canvas.roi.width == pytest.approx(0.45)
        source = tmp_path / "camera.mp4"
        make_video(source)
        project = create_project(tmp_path / "project", [source])
        project = project.model_copy(update={"settings": project.settings.model_copy(update={
            "webcam_enabled": True, "webcam_rois": {project.sources[0].id: ROI},
        })})
        window.restore_square_layout(project)
        assert window.webcam_enabled.isChecked()
        assert window.webcam_rois[source.resolve()] == ROI
        assert window.form_draft()["webcam_enabled"] is True
        window.folder.setText(str(tmp_path / "project"))
        window.set_sources([source])
        window.save_current_project()
        deadline = monotonic() + 15
        while window.job is not None and monotonic() < deadline:
            app.processEvents()
        assert window.job is None
        saved = load_project(tmp_path / "project")
        assert saved.settings.webcam_enabled
        assert saved.settings.webcam_rois[saved.sources[0].id] == ROI
        window.webcam_enabled.setChecked(False)
        assert window.webcam_rois[source.resolve()] == ROI
    finally:
        canvas.close()
        window.close()
        app.processEvents()


def test_real_webcam_dialog_can_seek_and_select(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    source = tmp_path / "source.mp4"
    make_video(source)
    dialog = WebcamDialog(source, ROI)
    try:
        dialog.show()
        deadline = monotonic() + 10
        while dialog.canvas is None and monotonic() < deadline:
            app.processEvents()
        assert dialog.canvas is not None
        assert dialog.roi == ROI
        dialog.seek.setValue(70)
        dialog.load_frame()
        deadline = monotonic() + 10
        while dialog.loading.isVisible() and monotonic() < deadline:
            app.processEvents()
        assert not dialog.loading.isVisible()
        assert dialog.roi == ROI
        dialog.confirm()
        assert dialog.result() == 1
    finally:
        dialog.close()
        app.processEvents()
