"""Small, demand-driven JPEG requests for the episode editor only."""

from collections import OrderedDict, deque
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QWidget

from sculptcut.i18n import tr
from sculptcut.media import discover
from sculptcut.timeline_preview import FrameWorker

ImageKey = tuple[Path, int, int, int]


def image_key(path: Path, microseconds: int) -> ImageKey:
    stat = path.stat()
    return path.resolve(), microseconds, stat.st_size, stat.st_mtime_ns


class EpisodeImages(QObject):
    ready = Signal(object, object)
    failed = Signal(str)

    def __init__(self, parent: QWidget, *, limit: int = 48, width: int = 192,
                 intermediate: bool = False) -> None:
        super().__init__(parent)
        self.owner = parent
        self.limit, self.width = limit, width
        self.intermediate = intermediate
        self.cache: OrderedDict[ImageKey, QImage] = OrderedDict()
        self.pending: deque[ImageKey] = deque()
        self.wanted: set[ImageKey] = set()
        self.worker: FrameWorker | None = None
        self.paused = False
        self.closed = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.start_next)

    def request(self, keys: list[ImageKey]) -> None:
        if self.closed:
            return
        self.wanted = set(keys)
        self.pending = deque(dict.fromkeys(keys))
        self.timer.start(0)

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        if not paused:
            self.timer.start(0)

    def start_next(self) -> None:
        if self.closed or self.paused or self.worker is not None:
            return
        while self.pending:
            key = self.pending.popleft()
            if key in self.cache:
                self.cache.move_to_end(key)
                self.ready.emit(key, self.cache[key])
                continue
            path, us, _, _ = key
            try:
                executable = discover("ffmpeg")
            except RuntimeError as exc:
                self.failed.emit(str(exc))
                return
            worker = FrameWorker([
                executable, "-v", "error", "-ss", str(us / 1e6), "-i", str(path),
                "-an", "-sn", "-frames:v", "1", "-vf",
                f"scale={self.width}:{self.width}:force_original_aspect_ratio=decrease",
                "-threads", "1", "-f", "image2pipe",
                "-vcodec", "mjpeg", "pipe:1",
            ], self.owner)
            self.worker = worker
            worker.ready.connect(lambda data, key=key: self.received(key, data))
            worker.finished.connect(self.finished)
            worker.start()
            return

    def received(self, key: ImageKey, data: bytes) -> None:
        if self.closed:
            return
        image = QImage.fromData(data)
        if image.isNull():
            if key in self.wanted:
                self.failed.emit(tr("Кадр недоступен; проверьте исходное видео."))
            return
        # Width and height are both bounded, including portrait sources.
        from PySide6.QtCore import Qt

        image = image.scaled(self.width, self.width, Qt.AspectRatioMode.KeepAspectRatio)
        self.cache[key] = image
        while len(self.cache) > self.limit:
            self.cache.popitem(last=False)
        if (key in self.wanted or self.intermediate and any(
                key[0] == wanted[0] and key[2:] == wanted[2:] for wanted in self.wanted)):
            self.ready.emit(key, image)

    def finished(self) -> None:
        if self.worker is not None:
            self.worker.deleteLater()
        self.worker = None
        if self.pending and not self.closed:
            self.timer.start(0)

    def shutdown(self) -> None:
        self.closed = True
        self.timer.stop()
        self.pending.clear()
        if self.worker is not None:
            self.worker.requestInterruption()
            self.worker.wait()
        self.cache.clear()
