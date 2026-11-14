"""Music-style source review lane, retaining the existing montage edit signals."""
from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtGui import (
    QColor,
    QFocusEvent,
    QHideEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
)

from sculptcut.i18n import tr
from sculptcut.planning import RenderDecision
from sculptcut.simple_timeline import EditMarker, SimpleTimeline
from sculptcut.time_format import format_time
from sculptcut.timeline_appearance import LanePiece, lane_bounds, paint_lane


def video_piece(row: int, start_ms: float, decision: RenderDecision, fps: int,
                source_name: str) -> LanePiece:
    duration = decision.output_frame_count/fps
    source_range = (f'{format_time(decision.source_start_us/1e6, 1)}–'
                    f'{format_time(decision.source_end_us/1e6, 1)}')
    if decision.action == 'HOLD':
        mode = tr('Стоп-кадр')
        source_detail = tr('Кадр исходника: {0}').format(
            format_time(decision.source_start_us/1e6, 1))
    elif decision.action == 'MONTAGE':
        mode = tr('Кадры')
        source_detail = tr('Серия кадров: {0}').format(len(decision.sample_times_us))
        source_detail += '\n'+tr('В исходнике: {0}').format(source_range)
    else:
        speed = (decision.source_end_us-decision.source_start_us)/1e6/duration
        value = f'{speed:.1f}'.rstrip('0').rstrip('.')
        mode = ('≈×' if decision.speed_ramp else '×')+value
        source_detail = tr('В исходнике: {0}').format(source_range)
    caption = tr('Стоп') if decision.action == 'HOLD' else mode
    short = f'{row+1} · {caption}'
    detail = tr('В ролике: {0}–{1} · {2} с').format(
        format_time(start_ms/1000, 1), format_time(start_ms/1000+duration, 1),
        f'{duration:.1f}')
    tooltip = '\n'.join((tr('Эпизод {0}').format(row+1), source_name, source_detail,
                         detail, mode))
    return LanePiece(row, start_ms, start_ms+duration*1000,
                     short, detail, short, detail, str(row+1), tooltip)


class ReviewTimeline(SimpleTimeline):
    marker_y = 86

    def __init__(self) -> None:
        super().__init__()
        self.pieces: list[LanePiece] = []
        self.setFixedHeight(104)
        self.setStyleSheet('')
        self.navigation_tip = tr('Клик — кадр · края — подрезка\n'
            'Пробел — просмотр · Q — разделить · Delete — убрать\n'
            'Ctrl+Z — отменить · Ctrl+Y — повторить\n'
            'Колесо — прокрутка · Ctrl+колесо — масштаб · Esc — отменить жест')
        self.setToolTip(self.navigation_tip)
        self.setAccessibleName(tr('Дорожка подрезки ролика'))

    def paintEvent(self, event: QPaintEvent) -> None:
        pieces = self.pieces
        if self.drag_edge in ('start', 'end'):
            pieces = [replace(c, start_ms=min(self.drag_time, c.end_ms-self.frame_ms))
                if c.row == self.drag_row and self.drag_edge == 'start' else
                replace(c, end_ms=max(self.drag_time, c.start_ms+self.frame_ms))
                if c.row == self.drag_row else c for c in pieces]
        p = QPainter(self)
        paint_lane(p, self, pieces, self.selected_row)
        top, bottom = lane_bounds(self)
        if not self.compact_lane:
            for value in (self.movie_start, self.movie_end or self.maximum()):
                p.fillRect(round(self.x_at(value))-4, 1, 8, 18, QColor('#8ed5ca'))
        for row, start, end in self.blocks:
            if row == self.selected_row and self.x_at(end)-self.x_at(start) >= 24:
                for value in (start, end):
                    p.fillRect(round(self.x_at(value))-4, top-4, 8, 4, QColor('#bbdfd9'))
        for edit in self.edits:
            a, b = self.edit_bounds(edit)
            if b < 0 or a > self.width():
                continue
            color = ('#727a86' if edit.inherited else '#a3aab3') if edit.removed else (
                '#428272' if edit.inherited else '#35aa80') if edit.kind == 'added' else (
                '#8c816a' if edit.inherited else '#d1b57c')
            p.fillRect(round(a), self.marker_y-4, max(3, round(b-a)), 8, QColor(color))
        for ms in self.split_positions:
            x = round(self.x_at(ms))
            p.fillRect(x-3, top, 7, bottom-top, QColor('#111923'))
            p.fillRect(x-1, top, 3, bottom-top, QColor('#73d8e8'))

    def edits_at(self, x: float, y: float) -> list[EditMarker]:
        if self.marker_y-5 <= y <= self.marker_y+5:
            return super().edits_at(x, 51)
        return []

    def handle_at(self, x: float, y: float) -> tuple[int, str] | None:
        top, bottom = lane_bounds(self)
        if not ((top-4 <= y <= top+6 or bottom-8 <= y <= bottom) if self.compact_lane else (
                18 <= y <= 27 or 47 <= y <= 72)):
            return None
        candidates = []
        for row, a, b in self.blocks:
            if self.x_at(b)-self.x_at(a) < 24:
                continue
            distance, edge = min((abs(x-self.x_at(a)), 'start'), (abs(x-self.x_at(b)), 'end'))
            if distance <= 8:
                candidates.append((distance, row != self.selected_row, row, edge))
        if candidates:
            _, _, row, edge = min(candidates)
            return row, edge
        return None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self.compact_lane and event.position().x() < self.left_padding-8:
            self.setFocus()
            return
        if self.pointer_down or self.pan_x is not None:
            return
        handle = self.handle_at(event.position().x(), event.position().y())
        if event.button() == Qt.MouseButton.LeftButton and handle:
            self.setFocus()
            self.drag_row, self.pending_edge = handle
            self.selected_row = self.drag_row
            self.press_x = event.position().x()
            self.drag_time = self.time_at(self.press_x)
            self.drag_edge = ''
            self.pointer_down = True
            self.update()
            return
        super().mousePressEvent(event)
        if self.compact_lane and self.pending_edge.startswith('movie'):
            self.pending_edge = ''
            self.movie_start = self.movie_end = 0

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.compact_lane and not self.pointer_down and event.position().y() < 8:
            self.setCursor(Qt.CursorShape.CrossCursor)
            self.setToolTip(self.navigation_tip)
            return
        if not self.pointer_down and self.pan_x is None and self.handle_at(
                event.position().x(), event.position().y()):
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            self.setToolTip(tr('Потяните край и отпустите, чтобы подрезать. Esc — отмена.'))
            return
        super().mouseMoveEvent(event)
        top, bottom = lane_bounds(self)
        x, y = event.position().x(), event.position().y()
        if (not self.pointer_down and self.pan_x is None and top <= y <= bottom
                and self.left_padding <= x <= self.width()-10 and not self.edit_at(x, y)):
            ms = self.time_at(x)
            piece = next((c for c in self.pieces if c.start_ms <= ms < c.end_ms), None)
            if piece is not None and piece.tooltip:
                self.setToolTip(piece.tooltip)

    def cancel_gesture(self) -> None:
        if self.pointer_down or self.pan_x is not None:
            if self.pan_x is not None:
                self.set_view(self.pan_start, self.view_span)
            self.pointer_down = False
            self.pan_x = None
            self.pending_edge = self.drag_edge = ''
            self.movie_start = self.movie_end = 0
            self.setSliderDown(False)
            self.update()
            self.scrub_finished.emit()

    def focusOutEvent(self, event: QFocusEvent) -> None:
        self.cancel_gesture()
        super().focusOutEvent(event)

    def hideEvent(self, event: QHideEvent) -> None:
        self.cancel_gesture()
        super().hideEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        self.cancel_gesture()
        if event.button() == Qt.MouseButton.LeftButton:
            self.seek_at(event)
            self.scrub_finished.emit()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if (event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace)
                and event.modifiers() != Qt.KeyboardModifier.NoModifier):
            event.accept()
            return
        super().keyPressEvent(event)
