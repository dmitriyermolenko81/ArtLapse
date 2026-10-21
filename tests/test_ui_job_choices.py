"""Read-only job controls retain selection, and one webcam button edits the ROI."""

from pathlib import Path
from threading import Event

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QListWidgetItem, QPushButton

from sculptcut.domain import Roi
from sculptcut.ui import Job, MainWindow


@pytest.mark.parametrize("shorts", [False, True])
def test_choices_stay_highlighted_during_real_background_job(shorts: bool) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    release = Event()
    job = Job(lambda cancel, progress, stage: "" if release.wait(10) else "timeout")
    try:
        window.preset.setCurrentIndex(window.preset.findData("shorts" if shorts else "timelapse"))
        window.frame_mode.setCurrentIndex(window.frame_mode.findData("viewport"))
        window.shorts_layout.setCurrentIndex(window.shorts_layout.findData("square"))
        window.music_selection.setCurrentIndex(window.music_selection.findData("full_tracks"))
        window.show()
        QTest.qWait(50)
        selectors = [window.preset, window.music_selection,
                     window.shorts_layout if shorts else window.frame_mode]
        values = [s.currentData() for s in selectors]
        colors = [s.buttons[s.currentData()].grab().toImage().pixelColor(20, 5)
                  for s in selectors]
        window.job = job
        job.finished.connect(window.job_finished)
        window.begin_job("Проверка интерфейса…")
        QTest.qWait(50)
        assert [s.currentData() for s in selectors] == values
        for selector, color in zip(selectors, colors, strict=True):
            selected = selector.buttons[selector.currentData()]
            assert selected.isChecked() and not selected.isEnabled()
            assert selected.grab().toImage().pixelColor(20, 5) == color
            other = next(b for b in selector.buttons.values() if b is not selected)
            assert other.grab().toImage().pixelColor(20, 5) != color
            QTest.mouseClick(other, Qt.MouseButton.LeftButton)
        assert [s.currentData() for s in selectors] == values
        release.set()
        assert job.wait(2000)
        app.processEvents()
        assert window.job is None
        assert [s.currentData() for s in selectors] == values
        assert all(s.isEnabled() for s in selectors)
    finally:
        release.set()
        job.wait(2000)
        app.processEvents()
        window.close()
        app.processEvents()


def test_one_webcam_button_opens_edits_cancels_and_disables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    source = tmp_path / "camera.mp4"
    item = QListWidgetItem("camera.mp4")
    item.setData(Qt.ItemDataRole.UserRole, str(source))
    window.sources.addItem(item)
    roi = Roi(x=0.1, y=0.7, width=0.25, height=0.3)
    answers = iter([1, 0, 1, 2])
    supplied: list[Roi | None] = []
    frame = QImage(160, 90, QImage.Format.Format_RGB888)
    frame.fill(Qt.GlobalColor.gray)
    window.source_media_cache[str(source)] = (frame, "")

    class Dialog(QDialog):
        def __init__(self, path: Path, previous: Roi | None, parent: MainWindow, *,
                     initial_frame: QImage | None = None) -> None:
            super().__init__(parent)
            assert path == source
            assert initial_frame is frame
            supplied.append(previous)
            self.roi = roi

        def exec(self) -> int:
            return next(answers)

    monkeypatch.setattr("sculptcut.ui.WebcamDialog", Dialog)
    try:
        window.show()
        app.processEvents()
        button = window.webcam_enabled
        assert isinstance(button, QPushButton) and not button.icon().isNull()
        assert not any(b.text() == "Область…" for b in window.findChildren(QPushButton))
        for expected in [True, True, True, False]:
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            assert button.isChecked() is expected
        assert supplied == [None, roi, roi, roi]
        assert window.webcam_rois[source.resolve()] is None
        assert window.form_draft()["webcam_enabled"] is False
    finally:
        window.close()
        app.processEvents()
