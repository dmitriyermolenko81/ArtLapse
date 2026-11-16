"""Check real Windows identity and window icons, including a child dialog."""

import ctypes
import os
import subprocess
import sys
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from sculptcut import __version__
from sculptcut.application_identity import (
    APP_USER_MODEL_ID,
    configure_application_identity,
    windows_application_id,
)
from sculptcut.ui import MainWindow


@pytest.mark.skipif(sys.platform != "win32", reason="Windows taskbar integration")
def test_normal_gui_entrypoint_sets_identity_before_show(tmp_path: Path) -> None:
    script = """
from PySide6.QtCore import QTimer
from artlapse.ui import main
from sculptcut import ui
from sculptcut.application_identity import APP_USER_MODEL_ID, windows_application_id

class ObservedWindow(ui.MainWindow):
    def show(self):
        assert windows_application_id() == APP_USER_MODEL_ID
        assert ui.QApplication.applicationName() == 'ArtLapse'
        super().show()
        QTimer.singleShot(200, self.close)

ui.MainWindow = ObservedWindow
raise SystemExit(main())
"""
    env = os.environ.copy()
    env["SCULPTCUT_DATA_DIR"] = str(tmp_path / "startup-data")
    env["ARTLAPSE_DATA_DIR"] = str(tmp_path / "startup-data")
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows taskbar integration")
def test_real_windows_identity_and_native_window_icons() -> None:
    configure_application_identity()
    app = QApplication.instance() or QApplication([])
    assert app.applicationName() == "ArtLapse"
    assert app.applicationVersion() == __version__
    assert windows_application_id() == APP_USER_MODEL_ID
    window = MainWindow()
    dialog = QDialog(window)
    user = ctypes.WinDLL("user32")
    get_icon = user.SendMessageW
    get_icon.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    get_icon.restype = ctypes.c_ssize_t
    try:
        window.show()
        dialog.show()
        app.processEvents()
        for widget in (window, dialog):
            assert not widget.windowIcon().isNull()
            assert {16, 32, 48, 256} <= {
                size.width() for size in widget.windowIcon().availableSizes()
            }
            # WM_GETICON: both the title-bar/taskbar small icon and the large icon.
            assert get_icon(int(widget.winId()), 0x007F, 0, 0)
            assert get_icon(int(widget.winId()), 0x007F, 1, 0)
    finally:
        dialog.close()
        window.close()
        app.processEvents()


def test_bundled_multimedia_decodes_video(tmp_path):
    import time

    import cv2
    import numpy as np
    from PySide6.QtCore import QUrl
    from PySide6.QtMultimedia import QMediaPlayer, QVideoSink
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from sculptcut.application_identity import configure_multimedia_paths

    path = tmp_path / "player.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*"mp4v"), 10, (64, 64))
    assert writer.isOpened()
    for _ in range(10):
        writer.write(np.full((64, 64, 3), 120, np.uint8))
    writer.release()
    app = QApplication.instance() or QApplication([])
    configure_multimedia_paths()
    player, sink = QMediaPlayer(), QVideoSink()
    frames = []
    sink.videoFrameChanged.connect(lambda f: frames.append(f.isValid()))
    player.setVideoSink(sink)
    player.setSource(QUrl.fromLocalFile(str(path)))
    player.play()
    try:
        deadline = time.monotonic()+5
        while not any(frames) and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(20)
        assert player.isAvailable() and any(frames), player.errorString()
    finally:
        player.stop()
        player.setSource(QUrl())
