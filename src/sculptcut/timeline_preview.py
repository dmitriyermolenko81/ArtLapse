"""Silent hover thumbnails with an independent, paused video decoder."""

import subprocess
from collections.abc import Callable
from pathlib import Path
from time import monotonic

from PySide6.QtCore import QEvent, QPoint, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QMouseEvent, QPixmap
from PySide6.QtWidgets import QLabel, QSlider, QVBoxLayout, QWidget

from sculptcut.i18n import tr
from sculptcut.media import discover
from sculptcut.time_format import format_time


class FrameWorker(QThread):
    ready = Signal(bytes)

    def __init__(self, arguments: list[str], parent: QWidget) -> None:
        super().__init__(parent)
        self.arguments = arguments

    def run(self) -> None:
        try:
            if self.isInterruptionRequested():
                return
            with subprocess.Popen(
                self.arguments,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ) as process:
                deadline = monotonic() + 5
                while True:
                    if self.isInterruptionRequested() or monotonic() >= deadline:
                        process.kill()
                        process.communicate()
                        if not self.isInterruptionRequested():
                            self.ready.emit(b"")
                        return
                    try:
                        output, _ = process.communicate(timeout=0.05)
                        break
                    except subprocess.TimeoutExpired:
                        continue
                if not self.isInterruptionRequested():
                    self.ready.emit(output if process.returncode == 0 else b"")
        except OSError:
            if not self.isInterruptionRequested():
                self.ready.emit(b"")


class PreviewSlider(QSlider):
    seekRequested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setStyleSheet(
            "QSlider::groove:horizontal { height:3px; background:#c2c8d0; }"
            "QSlider::handle:horizontal { background:#18a7f5; width:14px; "
            "margin:-7px 0; }"
        )
        self.setMouseTracking(True)
        self.popup = QWidget(self, Qt.WindowType.ToolTip)
        self.popup.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.popup.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout = QVBoxLayout(self.popup)
        layout.setContentsMargins(5, 5, 5, 5)
        self.picture = QLabel(tr("Загрузка кадра…"))
        self.picture.setFixedSize(240, 135)
        self.picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.time_label = QLabel()
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.picture)
        layout.addWidget(self.time_label)
        self.source = QUrl()
        self.worker: FrameWorker | None = None
        self.requested_ms = -1
        self.requested_source = QUrl()
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(80)
        self.timer.timeout.connect(self.load_frame)
        self.hover_ms = 0
        self.source_time: Callable[[int], int] = lambda position: position

    def set_source(self, url: QUrl) -> None:
        self.timer.stop()
        if self.worker is not None:
            self.worker.requestInterruption()
        self.popup.hide()
        self.picture.clear()
        self.source = url
        self.requested_ms = -1

    def load_frame(self) -> None:
        if self.worker is not None:
            return
        if self.source.isEmpty() or not self.popup.isVisible():
            return
        self.requested_ms = self.hover_ms
        self.requested_source = QUrl(self.source)
        try:
            executable = str(Path(discover("ffmpeg")).resolve())
        except RuntimeError:
            self.picture.setText(tr("Кадр недоступен"))
            return
        self.worker = FrameWorker(
            [
                executable,
                "-v",
                "error",
                "-ss",
                str(self.source_time(self.hover_ms) / 1000),
                "-i",
                self.source.toLocalFile(),
                "-an",
                "-sn",
                "-frames:v",
                "1",
                "-vf",
                "scale=240:135:force_original_aspect_ratio=decrease",
                "-threads",
                "1",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "pipe:1",
            ],
            self,
        )
        self.worker.ready.connect(self.frame_ready)
        self.worker.finished.connect(self.worker_finished)
        self.worker.start()

    def worker_finished(self) -> None:
        if self.worker:
            self.worker.deleteLater()
        self.worker = None
        if self.popup.isVisible() and (self.hover_ms != self.requested_ms
                                       or self.source != self.requested_source):
            self.timer.start()

    def frame_ready(self, data: bytes) -> None:
        if self.requested_source != self.source:
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            self.picture.setPixmap(pixmap)
        elif self.popup.isVisible():
            self.picture.setText(tr("Кадр недоступен"))
        if self.popup.isVisible() and self.hover_ms != self.requested_ms:
            self.timer.start()

    def time_at(self, x: float) -> int:
        ratio = max(0.0, min(1.0, (x - 7) / max(1, self.width() - 15)))
        return round(self.minimum() + ratio * (self.maximum() - self.minimum()))

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        super().mouseMoveEvent(event)
        if self.maximum() <= 0 or self.source.isEmpty():
            return
        self.hover_ms = self.time_at(event.position().x())
        if event.buttons() & Qt.MouseButton.LeftButton:
            self.setValue(self.hover_ms)
            self.seekRequested.emit(self.hover_ms)
        self.show_hover(self.hover_ms, self.mapToGlobal(QPoint(int(event.position().x()), 0)))

    def show_hover(self, milliseconds: int, point: QPoint) -> None:
        if self.source.isEmpty():
            return
        self.hover_ms = milliseconds
        self.time_label.setText(format_time(self.hover_ms / 1000))
        self.popup.adjustSize()
        screen = self.screen().availableGeometry()
        x = max(
            screen.left(),
            min(point.x() - self.popup.width() // 2, screen.right() - self.popup.width()),
        )
        self.popup.move(x, max(screen.top(), point.y() - self.popup.height() - 8))
        self.popup.show()
        if not self.timer.isActive():
            self.timer.start()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            position = self.time_at(event.position().x())
            self.setValue(position)
            self.seekRequested.emit(position)
            event.accept()
            return
        super().mousePressEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self.hide_hover()
        super().leaveEvent(event)

    def hide_hover(self) -> None:
        self.timer.stop()
        self.popup.hide()

    def shutdown(self) -> None:
        self.timer.stop()
        self.popup.hide()
        if self.worker:
            self.worker.requestInterruption()
            self.worker.wait()
