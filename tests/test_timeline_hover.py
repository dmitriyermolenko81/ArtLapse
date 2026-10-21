"""Keep the original continuous hover behaviour after withdrawing the cache."""

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from time import monotonic, sleep

import pytest
from PySide6.QtCore import QPoint, QUrl
from PySide6.QtWidgets import QApplication

from sculptcut.media import discover, run_process
from sculptcut.timeline_preview import FrameWorker, PreviewSlider


@pytest.fixture
def hover_video(tmp_path: Path) -> Path:
    source = tmp_path / "миниатюра с пробелом.mp4"
    run_process([
        discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
        "testsrc2=s=160x90:r=30:d=4", "-c:v", "libx264", str(source),
    ])
    return source


def wait_for(app: QApplication, condition: Callable[[], bool]) -> None:
    deadline = monotonic() + 8
    while not condition() and monotonic() < deadline:
        app.processEvents()
        sleep(.005)
    assert condition()


def test_next_hover_keeps_existing_picture_visible(hover_video: Path) -> None:
    app = QApplication.instance() or QApplication([])
    slider = PreviewSlider()
    slider.setRange(0, 4000)
    slider.set_source(QUrl.fromLocalFile(str(hover_video)))
    slider.show()
    try:
        point = slider.mapToGlobal(QPoint(100, 0))
        slider.show_hover(200, point)
        wait_for(app, lambda: slider.worker is None and not slider.picture.pixmap().isNull())
        first = slider.picture.pixmap().cacheKey()
        slider.show_hover(2800, point)
        assert not slider.picture.pixmap().isNull()
        assert slider.picture.pixmap().cacheKey() == first
        assert not slider.picture.text()
        wait_for(app, lambda: slider.worker is None
                 and slider.picture.pixmap().cacheKey() != first)
        assert slider.value() == 0  # Hovering does not seek/play the video.
    finally:
        slider.shutdown()
        slider.close()
        app.processEvents()


def test_continuous_motion_loads_without_waiting_for_pointer_stop(
    hover_video: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    slider = PreviewSlider()
    slider.setRange(0, 4000)
    slider.set_source(QUrl.fromLocalFile(str(hover_video)))
    starts: list[float] = []
    original_start = FrameWorker.start

    def counted(worker: FrameWorker) -> None:
        starts.append(monotonic())
        original_start(worker)

    monkeypatch.setattr(FrameWorker, "start", counted)
    slider.show()
    try:
        point = slider.mapToGlobal(QPoint(100, 0))
        moving_until = monotonic() + 1
        position = 0
        saw_picture_while_moving = False
        while monotonic() < moving_until:
            slider.show_hover(position, point)
            app.processEvents()
            saw_picture_while_moving |= not slider.picture.pixmap().isNull()
            position = (position + 20) % 3500
            sleep(.005)
        assert starts and starts[0] < moving_until
        assert saw_picture_while_moving
        assert slider.value() == 0
    finally:
        slider.shutdown()
        slider.close()
        app.processEvents()


def test_shutdown_interrupts_and_reaps_unneeded_decoder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    slider = PreviewSlider()
    processes: list[subprocess.Popen[bytes]] = []
    popen = subprocess.Popen

    def tracked(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr("sculptcut.timeline_preview.subprocess.Popen", tracked)
    worker = FrameWorker([sys.executable, "-c", "import time; time.sleep(5)"], slider)
    slider.worker = worker
    ready: list[bytes] = []
    worker.ready.connect(ready.append)
    worker.start()
    try:
        wait_for(app, lambda: bool(processes))
        started = monotonic()
        slider.shutdown()
        assert monotonic() - started < 1
        app.processEvents()
        assert processes[0].poll() is not None
        assert not ready
    finally:
        worker.requestInterruption()
        worker.wait()
        slider.close()
