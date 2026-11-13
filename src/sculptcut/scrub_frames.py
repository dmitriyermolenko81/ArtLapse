"""Latest-request-only CPU frame decoding, isolated from the playback backend."""
from collections import OrderedDict
from os import cpu_count
from pathlib import Path
from threading import Condition

import cv2
from PySide6.QtCore import QCoreApplication, QObject, QThread, Signal
from PySide6.QtGui import QImage

from sculptcut.episode_images import ImageKey
from sculptcut.quick_seek import QuickSeekWorker, cached_path, start_copy

_workers: set["ScrubDecoder"] = set()


def wait_for_workers() -> None:
    # Dialogs may already be deleted when the application quits. The worker
    # registry, rather than a dialog-owned slot, must finish decoder teardown.
    for worker in list(_workers):
        worker.stop()
    for worker in list(_workers):
        worker.wait(3000)


class ScrubDecoder(QThread):
    decoder_threads = min(4, cpu_count() or 1)
    forward_decode_seconds = 1
    decoded = Signal(object, object)
    failed = Signal(object, str)

    def __init__(self) -> None:
        super().__init__()
        self.condition = Condition()
        self.pending: tuple[ImageKey, bool] | None = None
        self.stopping = False
        self.proxies: dict[Path, Path] = {}

    def use_proxy(self, original: Path, proxy: Path) -> None:
        with self.condition:
            self.proxies[original] = proxy

    def request(self, key: ImageKey, forward: bool = False) -> None:
        with self.condition:
            self.pending = key, forward
            self.condition.notify()

    def stop(self) -> None:
        with self.condition:
            self.stopping = True
            self.pending = None
            self.condition.notify()

    def run(self) -> None:
        capture = None
        current = None
        try:
            while True:
                with self.condition:
                    while self.pending is None and not self.stopping:
                        self.condition.wait()
                    if self.stopping:
                        return
                    request, self.pending = self.pending, None
                assert request is not None
                key, forward = request
                path, us, size, modified = key
                with self.condition:
                    proxy = self.proxies.get(path)
                if proxy is not None:
                    try:
                        stat = proxy.stat()
                    except OSError:
                        proxy = None
                    else:
                        path = proxy
                        size, modified = stat.st_size, stat.st_mtime_ns
                identity = path, size, modified
                if current != identity:
                    if capture is not None:
                        capture.release()
                    capture = cv2.VideoCapture(str(path), cv2.CAP_FFMPEG,
                        [cv2.CAP_PROP_N_THREADS, self.decoder_threads])
                    current = identity
                assert capture is not None
                fps = capture.get(cv2.CAP_PROP_FPS)
                target = round(us*fps/1e6)
                following = round(capture.get(cv2.CAP_PROP_POS_FRAMES))
                if (forward and proxy is None and fps > 0
                        and 0 <= target-following <= fps*self.forward_decode_seconds):
                    # Nearby forward clicks can continue the current GOP instead
                    # of seeking back and decoding it a second time.
                    for _ in range(target-following):
                        if not capture.grab():
                            break
                else:
                    capture.set(cv2.CAP_PROP_POS_MSEC, us/1000)
                ok, frame = capture.read()
                with self.condition:
                    if self.stopping:
                        return
                    if self.pending is not None and self.pending[0] != key:
                        continue
                if not ok:
                    if proxy is not None:
                        with self.condition:
                            self.proxies.pop(key[0], None)
                            if self.pending is None:
                                self.pending = key, forward
                        current = None
                        continue
                    self.failed.emit(key, "Кадр недоступен")
                    continue
                h, w = frame.shape[:2]
                scale = min(1.0, 720/max(w, h))
                if scale < 1:
                    frame = cv2.resize(frame, (max(1, round(w*scale)), max(1, round(h*scale))))
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                image = QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0],
                               QImage.Format.Format_RGB888).copy()
                self.decoded.emit(key, image)
        except (OSError, ValueError, cv2.error) as exc:
            if current is not None:
                self.failed.emit(key, str(exc))
        finally:
            if capture is not None:
                capture.release()


class ScrubFrames(QObject):
    ready = Signal(object, object)
    failed = Signal(str)
    quick_busy = Signal(bool)

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self.cache: OrderedDict[ImageKey, QImage] = OrderedDict()
        self.wanted: ImageKey | None = None
        self.closed = False
        self.quick_worker: QuickSeekWorker | None = None
        self.cleaned = False
        self.worker = ScrubDecoder()
        _workers.add(self.worker)
        self.worker.decoded.connect(self.received)
        self.worker.failed.connect(self.error)
        worker = self.worker
        worker.finished.connect(lambda: _workers.discard(worker))
        worker.finished.connect(lambda: setattr(self, "cleaned", True))
        worker.finished.connect(worker.deleteLater)
        app = QCoreApplication.instance()
        if app and not app.property("artlapseScrubShutdown"):
            app.setProperty("artlapseScrubShutdown", True)
            app.aboutToQuit.connect(wait_for_workers)
        worker.start()

    def prepare_video(self, video: Path) -> None:
        path = video.resolve()
        cached = cached_path(path)
        if cached.is_file():
            self.worker.use_proxy(path, cached)
            return
        self.quick_busy.emit(True)
        worker = start_copy(path)
        self.quick_worker = worker
        worker.ready.connect(self.proxy_ready)
        worker.finished.connect(lambda: setattr(self, 'quick_worker', None))
        worker.finished.connect(lambda: self.quick_busy.emit(False) if not self.closed else None)
        worker.start()

    def proxy_ready(self, original: Path, proxy: Path) -> None:
        if not self.closed:
            self.worker.use_proxy(original, proxy)

    def request(self, key: ImageKey, *, forward: bool = False) -> None:
        if self.closed:
            return
        self.wanted = key
        if key in self.cache:
            self.cache.move_to_end(key)
            self.ready.emit(key, self.cache[key])
        else:
            self.worker.request(key, forward)

    def received(self, key: ImageKey, image: QImage) -> None:
        if self.closed:
            return
        self.cache[key] = image
        while len(self.cache) > 24:
            self.cache.popitem(last=False)
        if key == self.wanted:
            self.ready.emit(key, image)

    def error(self, key: ImageKey, message: str) -> None:
        if not self.closed and key == self.wanted:
            self.failed.emit(message)

    def shutdown(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.worker.stop()
        if self.quick_worker is not None:
            self.quick_worker.cancel.set()
        self.cache.clear()
