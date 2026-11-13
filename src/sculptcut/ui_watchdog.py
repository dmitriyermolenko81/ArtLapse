"""Capture the Python call site if the GUI stops servicing its event loop."""
import logging
import sys
from threading import Event, Thread, main_thread
from time import monotonic
from traceback import format_stack

from PySide6.QtCore import QCoreApplication, QObject, QTimer


class UiWatchdog(QObject):
    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self.last_beat = monotonic()
        self.stopped = Event()
        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.beat)
        self.timer.start()
        self.worker = Thread(target=self.observe, name='artlapse-ui-watchdog', daemon=True)
        self.worker.start()
        app = QCoreApplication.instance()
        if app:
            app.aboutToQuit.connect(self.stop)
        parent.destroyed.connect(self.stop)

    def beat(self) -> None:
        self.last_beat = monotonic()

    def observe(self) -> None:
        reported = False
        while not self.stopped.wait(0.25):
            delay = monotonic()-self.last_beat
            if delay < 1:
                reported = False
            elif not reported:
                frame = sys._current_frames().get(main_thread().ident or 0)
                stack = ''.join(format_stack(frame)) if frame is not None else 'unavailable'
                logging.getLogger('artlapse').warning(
                    'GUI event loop stalled for %.2fs\n%s', delay, stack,
                    extra={'event': 'gui_stall'})
                reported = True

    def stop(self) -> None:
        self.stopped.set()
        self.timer.stop()
