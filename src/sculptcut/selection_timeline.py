"""Zoomable source coverage with a free, draggable selection."""

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent, QWheelEvent
from PySide6.QtWidgets import QWidget

from sculptcut.i18n import tr
from sculptcut.planning import RenderDecision
from sculptcut.selection_history import ManualEdit
from sculptcut.time_format import format_time


class SelectionTimeline(QWidget):
    # Source timestamps are microseconds; Qt int overflows after 35m47s.
    selected = Signal(object, object)
    scrubbed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.duration = 1
        self.view_start, self.view_end = 0, 1
        self.start, self.end = 0, 1
        self.bands: list[RenderDecision] = []
        self.edits: list[ManualEdit] = []
        self.drag = ""
        self.anchor = 0
        self.playhead_us = 0
        self.show_caption = True
        self.setMinimumHeight(96)
        self.setAccessibleName(tr("Исходник: оставленные участки и пропуски"))
        self.setToolTip(
            tr(
                "Клик — показать кадр. Границы меняются только за две ручки. "
                "Колесо — масштаб, Shift+колесо — сдвиг."
            )
        )

    def x_at(self, us: int) -> float:
        return 10 + (us - self.view_start) / max(1, self.view_end - self.view_start) * (
            self.width() - 20
        )

    def time_at(self, x: float) -> int:
        return max(
            0,
            min(
                self.duration,
                round(
                    self.view_start
                    + (x - 10) / max(1, self.width() - 20) * (self.view_end - self.view_start)
                ),
            ),
        )

    def set_view(self, start: int, span: int) -> None:
        span = min(self.duration, max(min(1_000_000, self.duration), span))
        self.view_start = max(0, min(start, self.duration - span))
        self.view_end = self.view_start + span
        self.update()

    def focus(self, start: int, end: int) -> None:
        self.start, self.end = start, end
        span = min(self.duration, max(30_000_000, (end - start) * 3))
        self.set_view((start + end - span) // 2, span)

    def zoom(self, factor: float) -> None:
        span = round((self.view_end - self.view_start) * factor)
        self.set_view((self.start + self.end - span) // 2, span)

    def wheelEvent(self, event: QWheelEvent) -> None:
        steps = event.angleDelta().y() / 120
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.set_view(
                self.view_start - round(steps * (self.view_end - self.view_start) * 0.15),
                self.view_end - self.view_start,
            )
        else:
            self.zoom(1.4 ** (-max(-5, min(5, steps))))
        event.accept()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#111923"))
        painter.setPen(QColor("#a4b4ca"))
        for i in range(5):
            us = self.view_start + (self.view_end - self.view_start) * i // 4
            x = round(self.x_at(us))
            painter.drawText(max(5, min(self.width() - 80, x - 25)), 20, format_time(us / 1e6, 1))
        for d in self.bands:
            if d.source_end_us <= self.view_start or d.source_start_us >= self.view_end:
                continue
            color = (
                "#36404d"
                if not d.output_frame_count
                else "#476ca7"
            )
            a, b = (
                max(10, self.x_at(d.source_start_us)),
                min(self.width() - 10, self.x_at(d.source_end_us)),
            )
            painter.fillRect(QRectF(a, 32, max(1, b - a - 1), 32), QColor(color))
        for edit in self.edits:
            a, b = max(10, self.x_at(edit.start_us)), min(self.width()-10, self.x_at(edit.end_us))
            if a >= b:
                continue
            color = '#9ba2ac' if edit.kind == 'removed' else (
                '#35aa80' if edit.kind == 'added' else '#d1b57c')
            painter.fillRect(QRectF(a, 32, b-a, 32), QColor(color))
            if edit.kind == 'removed':
                painter.setPen(QColor('#626b78'))
                for x in range(round(a), round(b), 8):
                    painter.drawLine(x, 63, min(x+16, round(b)), 33)
        a, b = self.x_at(self.start), self.x_at(self.end)
        painter.setPen(QColor("#f5ce80"))
        painter.setBrush(QColor(245, 206, 128, 45))
        painter.drawRect(QRectF(a, 29, max(1, b - a), 38))
        for handle_x in (a, b):
            painter.fillRect(QRectF(handle_x - 3, 27, 6, 42), QColor("#f5ce80"))
        painter.setPen(QColor("#e4d7b6"))
        painter.drawLine(round(self.x_at(self.playhead_us)), 24,
                         round(self.x_at(self.playhead_us)), 72)
        if not self.show_caption:
            return
        painter.drawText(
            10,
            88,
            tr("Выделено: {0}–{1}").format(
                format_time(self.start / 1e6, 2), format_time(self.end / 1e6, 2)
            ),
        )

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        x = event.position().x()
        in_handles = 27 <= event.position().y() <= 69
        distances = [(abs(x - self.x_at(self.start)), "start"),
                     (abs(x - self.x_at(self.end)), "end")]
        distance, edge = min(distances)
        self.drag = edge if in_handles and distance < 9 else "scrub"
        self.mouseMoveEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not self.drag:
            return
        time = self.time_at(event.position().x())
        if self.drag == "start":
            self.start = max(0, min(time, self.end - 10_000))
            time = self.start
        elif self.drag == "end":
            self.end = min(self.duration, max(time, self.start + 10_000))
            time = max(self.start, self.end - 1)
        self.playhead_us = time
        self.scrubbed.emit(time)
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if not self.drag or event.button() != Qt.MouseButton.LeftButton:
            return
        self.mouseMoveEvent(event)
        edge = self.drag
        self.drag = ""
        if edge in ("start", "end") and self.start < self.end:
            target = self.playhead_us
            self.selected.emit(self.start, self.end)
            # Keep the frame at the edited edge after synchronizing the controls.
            self.scrubbed.emit(target)
        self.update()
