from pathlib import Path
from threading import Event
from time import monotonic, sleep

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from sculptcut import crop_dialog
from sculptcut.crop_dialog import CropCanvas, TimelapseCropDialog, WebcamDialog
from sculptcut.domain import Roi
from sculptcut.media import discover, run_process


def test_crop_rectangle_is_drawn_by_user() -> None:
    app = QApplication.instance() or QApplication([])
    image = QPixmap(400, 200)
    image.fill(Qt.GlobalColor.gray)
    canvas = CropCanvas(image, Roi(x=0.1, y=0.1, width=0.75, height=0.8))
    try:
        canvas.show()
        app.processEvents()
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseMove(canvas, QPoint(360, 180))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(360, 180))
        assert canvas.roi.x == pytest.approx(0.05)
        assert canvas.roi.y == pytest.approx(0.1)
        assert canvas.roi.height == pytest.approx(0.9)
        assert canvas.roi.width * 400 / (canvas.roi.height * 200) == pytest.approx(16 / 9)
    finally:
        canvas.close()
        app.processEvents()


def test_crop_rectangle_moves_inside_frame_without_resizing() -> None:
    app = QApplication.instance() or QApplication([])
    image = QPixmap(400, 200)
    image.fill(Qt.GlobalColor.gray)
    canvas = CropCanvas(image, Roi(x=0.1, y=0.1, width=0.5, height=0.5))
    try:
        canvas.show()
        app.processEvents()
        original = canvas.roi
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(120, 60))
        QTest.mouseMove(canvas, QPoint(160, 80))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(160, 80))
        assert canvas.roi.x == pytest.approx(original.x + 0.1)
        assert canvas.roi.y == pytest.approx(original.y + 0.1)
        assert canvas.roi.width == original.width
        assert canvas.roi.height == original.height

        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(160, 80))
        QTest.mouseMove(canvas, QPoint(399, 199))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(399, 199))
        assert canvas.roi.x == pytest.approx(1 - original.width)
        assert canvas.roi.y == pytest.approx(1 - original.height)
    finally:
        canvas.close()
        app.processEvents()


def test_crop_rectangle_resizes_from_corner_at_fixed_aspect() -> None:
    app = QApplication.instance() or QApplication([])
    image = QPixmap(400, 200)
    image.fill(Qt.GlobalColor.gray)
    canvas = CropCanvas(image, Roi(x=0.2, y=0.2, width=0.4, height=0.45))
    try:
        canvas.show()
        app.processEvents()
        original = canvas.roi
        corner = QPoint(round((original.x + original.width) * 400),
                        round((original.y + original.height) * 200))
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=corner)
        QTest.mouseMove(canvas, QPoint(280, 152))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(280, 152))
        assert canvas.roi.x == pytest.approx(original.x)
        assert canvas.roi.y == pytest.approx(original.y)
        assert canvas.roi.width > original.width
        assert canvas.roi.height > original.height
        assert canvas.roi.width * 400 / (canvas.roi.height * 200) == pytest.approx(16 / 9)
    finally:
        canvas.close()
        app.processEvents()


def test_crop_dialog_opens_real_video_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "video.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=160x90:r=10:d=2",
            "-c:v",
            "libx264",
            str(source),
        ]
    )
    app = QApplication.instance() or QApplication([])
    roi = Roi(x=0.1, y=0.1, width=0.75, height=0.75)
    dialog = TimelapseCropDialog(source, roi)
    try:
        dialog.show()
        deadline = monotonic() + 5
        while dialog.canvas is None and monotonic() < deadline:
            app.processEvents()
            sleep(0.01)
        assert dialog.canvas is not None
        assert dialog.canvas.frame.width() > 0
        assert dialog.roi == roi
        cached_frame = dialog.canvas.frame.toImage()
    finally:
        dialog.close()
        app.processEvents()

    def unexpected_decode(_source: Path) -> QImage:
        pytest.fail("A cached crop frame must not start another video decoder")

    monkeypatch.setattr(crop_dialog, "preview_frame", unexpected_decode)
    cached = TimelapseCropDialog(source, roi, initial_frame=cached_frame)
    try:
        cached.show()
        app.processEvents()
        assert cached.canvas is not None
        assert not hasattr(cached, "loader")
        assert cached.roi == roi
        assert cached.buttons.button(cached.buttons.StandardButton.Ok).isEnabled()
        # Qt retranslates standard button labels after theme/locale changes.
        app.sendEvent(cached.buttons, QEvent(QEvent.Type.LanguageChange))
        assert cached.buttons.button(cached.buttons.StandardButton.Ok).text() == "Применить"
    finally:
        cached.close()
        app.processEvents()

    webcam = WebcamDialog(source, roi, initial_frame=cached_frame)
    try:
        webcam.show()
        app.processEvents()
        assert webcam.canvas is not None and webcam.roi == roi
        assert webcam.buttons.button(webcam.buttons.StandardButton.Ok).isEnabled()
    finally:
        webcam.close()
        app.processEvents()


def test_crop_dialog_does_not_block_while_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = QApplication.instance() or QApplication([])
    frame = QImage(160, 90, QImage.Format.Format_RGB888)
    frame.fill(Qt.GlobalColor.gray)

    def slow_preview(_source: Path) -> QImage:
        sleep(0.25)
        return frame

    monkeypatch.setattr(crop_dialog, "preview_frame", slow_preview)
    started = monotonic()
    dialog = TimelapseCropDialog(tmp_path / "source.mp4", Roi(x=0, y=0, width=1, height=1))
    try:
        assert monotonic() - started < 0.2
        dialog.show()
        app.processEvents()
        assert dialog.canvas is None
        assert not dialog.buttons.button(dialog.buttons.StandardButton.Ok).isEnabled()
        deadline = monotonic() + 2
        while dialog.canvas is None and monotonic() < deadline:
            app.processEvents()
            sleep(0.01)
        assert dialog.canvas is not None
        assert dialog.buttons.button(dialog.buttons.StandardButton.Ok).isEnabled()
    finally:
        dialog.close()
        app.processEvents()


def test_webcam_seeking_preserves_roi_and_ignores_result_after_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    frame = QImage(160, 90, QImage.Format.Format_RGB888)
    frame.fill(Qt.GlobalColor.gray)
    release, started = Event(), Event()
    fractions: list[float] = []

    def preview(_path: Path, fraction: float) -> QImage:
        fractions.append(fraction)
        started.set()
        assert release.wait(2)
        return frame

    monkeypatch.setattr(crop_dialog, "preview_frame", preview)
    roi = Roi(x=.1, y=.6, width=.2, height=.3)
    dialog = WebcamDialog(tmp_path / "source.mp4", roi, initial_frame=frame)
    dialog.show()
    try:
        assert dialog.canvas is not None and not fractions
        dialog.seek.setValue(25)
        dialog.load_frame()
        assert started.wait(1)
        release.set()
        deadline = monotonic() + 2
        while crop_dialog._active_loaders and monotonic() < deadline:
            app.processEvents()
            sleep(.005)
        app.processEvents()
        assert fractions == [.25] and dialog.roi == roi

        release.clear()
        started.clear()
        dialog.seek.setValue(50)
        dialog.load_frame()
        assert started.wait(1)
        late: list[QImage] = []
        monkeypatch.setattr(dialog, "show_frame", late.append)
        dialog.reject()
        release.set()
        deadline = monotonic() + 2
        while crop_dialog._active_loaders and monotonic() < deadline:
            app.processEvents()
            sleep(.005)
        app.processEvents()
        assert fractions == [.25, .5] and not late
    finally:
        release.set()
        for loader in list(crop_dialog._active_loaders):
            loader.wait(2500)
        dialog.close()
        app.processEvents()
