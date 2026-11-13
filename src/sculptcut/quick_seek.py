"""Persistent native-resolution all-intra copies for random access to rendered previews."""
import hashlib
import logging
from pathlib import Path
from tempfile import gettempdir
from threading import Event
from uuid import uuid4

import cv2
from PySide6.QtCore import QCoreApplication, QThread, Signal

from sculptcut.media import Cancelled, discover, run_process
from sculptcut.runtime_paths import data_directory

_workers: set["QuickSeekWorker"] = set()


def cached_path(video: Path) -> Path:
    stat = video.stat()
    key = hashlib.sha256(str((str(video.resolve()), stat.st_size, stat.st_mtime_ns,
                              'native-intra-crf18-v2')).encode()).hexdigest()
    directory = (data_directory() or Path(gettempdir())/'artlapse')/'seek-cache'
    return directory/f'scrub-v2-{key}.mp4'


def build_copy(video: Path, destination: Path, cancel: Event) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        return destination
    pending = destination.with_name(f'{destination.stem}-{uuid4().hex}.pending.mp4')
    try:
        run_process([discover('ffmpeg'), '-v', 'error', '-nostdin', '-y', '-threads', '2',
            '-i', str(video), '-map', '0:v:0', '-an', '-sn', '-dn', '-vf',
            'setsar=1',
            '-filter_threads', '1', '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '18',
            '-g', '1', '-keyint_min', '1', '-bf', '0', '-threads', '2',
            '-movflags', '+faststart', str(pending)], cancel)
        counts = []
        for path in (video, pending):
            capture = cv2.VideoCapture(str(path))
            try:
                counts.append((round(capture.get(cv2.CAP_PROP_FRAME_COUNT)),
                               capture.get(cv2.CAP_PROP_FPS)))
            finally:
                capture.release()
        if counts[0][0] <= 0 or counts[0] != counts[1]:
            raise ValueError('Quick-seek frame count or FPS mismatch')
        if cancel.is_set():
            raise Cancelled('Cancelled quick-seek copy')
        pending.replace(destination)
        # Only files belonging to this cache format; locked active copies stay.
        others = sorted(destination.parent.glob('scrub-v2-*.mp4'),
                        key=lambda p: p.stat().st_mtime_ns, reverse=True)
        for old in others[4:]:
            if old != destination and len(old.stem) == len('scrub-v2-')+64:
                try:
                    old.unlink()
                except PermissionError:
                    pass
        return destination
    finally:
        pending.unlink(missing_ok=True)


class QuickSeekWorker(QThread):
    ready = Signal(object, object)
    failed = Signal(str)

    def __init__(self, video: Path) -> None:
        super().__init__()
        self.video = video.resolve()
        self.cancel = Event()

    def run(self) -> None:
        try:
            result = build_copy(self.video, cached_path(self.video), self.cancel)
            if not self.cancel.is_set():
                self.ready.emit(self.video, result)
        except Cancelled:
            pass
        except (OSError, ValueError, RuntimeError) as exc:
            logging.getLogger('artlapse').warning('Quick-seek copy unavailable: %s', exc)
            self.failed.emit(str(exc))


def wait_workers() -> None:
    for worker in list(_workers):
        worker.cancel.set()
    for worker in list(_workers):
        worker.wait(3000)


def start_copy(video: Path) -> QuickSeekWorker:
    worker = QuickSeekWorker(video)
    _workers.add(worker)
    worker.finished.connect(lambda: _workers.discard(worker))
    worker.finished.connect(worker.deleteLater)
    app = QCoreApplication.instance()
    if app and not app.property('artlapseQuickSeekShutdown'):
        app.setProperty('artlapseQuickSeekShutdown', True)
        app.aboutToQuit.connect(wait_workers)
    return worker
