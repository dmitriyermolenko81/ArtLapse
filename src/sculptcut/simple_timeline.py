"""One montage track: seek, scene edges, and outer movie bounds."""

from dataclasses import dataclass

from PySide6.QtCore import QEvent, QSignalBlocker, Qt, Signal
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPainter, QPaintEvent, QWheelEvent
from PySide6.QtWidgets import QApplication, QMenu

from sculptcut.i18n import tr
from sculptcut.montage_timeline import MontageTimeline
from sculptcut.time_format import format_time


@dataclass(frozen=True)
class EditMarker:
    key: int
    start_ms: int
    end_ms: int
    removed: bool
    label: str
    kind: str = 'changed'
    inherited: bool = False
    detail: str = ''
    removed_ms: int = 0


class SimpleTimeline(MontageTimeline):
    view_changed = Signal(float, float)
    marker_y = 51
    edge_preview = Signal(int, str, int)
    edge_commit = Signal(int, str, int)
    movie_commit = Signal(int, int)
    remove_scene = Signal()
    split_scene = Signal()
    play_requested = Signal()
    undo_requested = Signal()
    redo_requested = Signal()
    scrub_finished = Signal()
    restore_edit = Signal(int)
    revise_edit = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self.selected_row = 0
        self.view_start = 0.0
        self.view_span = 0.0
        self.left_padding = 10
        self.compact_lane = False
        self.show_ruler = True
        self.lane_label = ''
        self.lane_icon = ''
        self.drag_edge = ""
        self.drag_row = 0
        self.drag_time = 0
        self.movie_start = 0
        self.movie_end = 0
        self.pending_edge = ""
        self.press_x = 0.0
        self.pointer_down = False
        self.pan_x: float | None = None
        self.pan_start = 0.0
        self.frame_ms = 1000 / 30
        self.edits: list[EditMarker] = []
        self.split_positions: list[int] = []
        self.edit_menu: QMenu | None = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedHeight(72)
        self.setStyleSheet("font-size:11px;")
        self.setToolTip(tr("Колесо — прокрутка\nCtrl+колесо — масштаб\nQ — разделить"))
        self.navigation_tip = self.toolTip()

    def set_view(self, start: float, span: float) -> None:
        previous = (self.view_start, self.view_span)
        total = max(1, self.maximum())
        self.view_span = max(min(1000, total), min(total, span))
        self.view_start = max(0, min(total-self.view_span, start))
        self.update()
        if previous != (self.view_start, self.view_span):
            self.view_changed.emit(self.view_start, self.view_span)

    def setRange(self, minimum: int, maximum: int) -> None:
        was_full = not self.view_span or self.view_span >= self.maximum()
        super().setRange(minimum, maximum)
        self.set_view(0 if was_full else self.view_start,
                      maximum if was_full else self.view_span)

    def navigate(self, ms: int) -> None:
        self.setValue(max(0, min(self.maximum(), ms)))
        span = self.view_span or self.maximum()
        if not self.view_start <= self.value() <= self.view_start + span:
            self.set_view(self.value()-span/2, span)
        self.sliderMoved.emit(self.value())

    def reset_view(self) -> None:
        self.set_view(0, self.maximum())

    def follow(self, ms: int, row: int | None = None) -> None:
        if self.pointer_down or self.pan_x is not None:
            return
        span = self.view_span or self.maximum()
        center = float(ms)
        if row is not None:
            block = next(((a, b) for r, a, b in self.blocks if r == row), None)
            if block:
                a, b = block
                center = (a+b)/2
                span = max(span, (b-a)*1.2)
        self.set_view(center-span/2, span)

    def x_at(self, ms: float) -> float:
        return self.left_padding + (ms - self.view_start) / max(
            1, self.view_span or self.maximum()) * (self.width()-self.left_padding-10)

    def time_at(self, x: float) -> int:
        return max(0, min(self.maximum(), round(self.view_start + (x-self.left_padding) /
            max(1, self.width()-self.left_padding-10) * (self.view_span or self.maximum()))))

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#111923"))
        total = self.maximum()
        p.setPen(QColor("#344653"))
        p.drawLine(round(self.x_at(0)), 8, round(self.x_at(total)), 8)
        for value in (self.movie_start, self.movie_end or total):
            p.fillRect(round(self.x_at(value)) - 4, 0, 8, 17, QColor("#8ed5ca"))
        for row, start, end in self.blocks:
            a, b = self.x_at(start), self.x_at(end)
            p.fillRect(round(a), 27, max(1, round(b-a)-1), 28,
                       QColor("#547fba" if row == self.selected_row else "#36465b"))
            if b-a > 24:
                p.setPen(QColor("#ffffff"))
                p.drawText(round(a)+5, 46, str(row+1))
            if row == self.selected_row:
                bounds = (self.drag_time if self.drag_edge == "start" else start,
                          self.drag_time if self.drag_edge == "end" else end)
                if b-a >= 24:
                    for value in bounds:
                        x = round(self.x_at(value))
                        p.fillRect(x-5, 18, 10, 9, QColor("#f5ce80"))
                        p.fillRect(x-1, 27, 2, 28, QColor("#f5ce80"))
        for edit in self.edits:
            a, b = self.x_at(edit.start_ms), self.x_at(edit.end_ms)
            if edit.removed:
                width = max(14, min(48, round(self.x_at(edit.start_ms+edit.removed_ms)-a)))
                left = round(a)-width//2
                p.fillRect(left, 47, width, 8, QColor('#727a86' if edit.inherited else '#a3aab3'))
                p.setPen(QColor('#444b56'))
                for x in range(left, left+width, 5):
                    p.drawLine(x, 54, min(x+5, left+width-1), 48)
            elif edit.kind == 'added':
                color = QColor('#428272' if edit.inherited else '#35aa80')
                p.fillRect(round(a), 47, max(3, round(b-a)), 8, color)
                color.setAlpha(60 if edit.inherited else 95)
                p.fillRect(round(a), 27, max(3, round(b-a)), 20, color)
            else:
                p.fillRect(round(a), 51, max(3, round(b-a)), 4,
                           QColor('#8c816a' if edit.inherited else '#d1b57c'))
        for seam_ms in self.split_positions:
            x = round(self.x_at(seam_ms))
            p.fillRect(x-3, 27, 7, 28, QColor('#111923'))
            p.fillRect(x-1, 27, 3, 28, QColor('#73d8e8'))
            for y in (27, 53):
                p.fillRect(x-3, y, 7, 2, QColor('#73d8e8'))
        p.setPen(QColor("#ffffff"))
        x = round(self.x_at(self.value()))
        p.drawLine(x, 19, x, 59)
        p.setPen(QColor("#a4b4ca"))
        span = self.view_span or total
        for i in range(5):
            ms = self.view_start + span*i/4
            p.drawText(max(0, min(self.width()-65, round(self.x_at(ms))-20)),
                       69, format_time(ms/1000, 1))

    def seek_at(self, event: QMouseEvent) -> None:
        with QSignalBlocker(self):
            self.setValue(self.time_at(event.position().x()))
        self.sliderMoved.emit(self.value())

    def edit_at(self, x: float, y: float) -> EditMarker | None:
        return next(iter(self.edits_at(x, y)), None)

    def edit_bounds(self, edit: EditMarker) -> tuple[float, float]:
        a = self.x_at(edit.start_ms)
        if edit.removed:
            width = max(14, min(48, round(self.x_at(edit.start_ms+edit.removed_ms)-a)))
            return a-width/2, a+width/2
        return a, max(a+3, self.x_at(edit.end_ms))

    def edits_at(self, x: float, y: float) -> list[EditMarker]:
        return sorted((e for e in self.edits if
            (27 if e.kind == 'added' else 47) <= y <= 57
            and self.edit_bounds(e)[0]-2 <= x <= self.edit_bounds(e)[1]+2),
            key=lambda e: not e.removed)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            if self.pointer_down:
                return
            self.setFocus()
            self.pan_x = event.position().x()
            self.pan_start = self.view_start
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        if self.pan_x is not None:
            return
        if event.button() == Qt.MouseButton.RightButton:
            edits = self.edits_at(event.position().x(), event.position().y())
            if edits:
                menu = QMenu(self)
                self.edit_menu = menu
                submenus = []
                commands = []
                picked: list[tuple[bool, int]] = []
                for edit in edits:
                    actions = menu.addMenu(edit.label) if len(edits) > 1 else menu
                    submenus.append(actions)
                    commands.append(actions.menuAction())
                    caption = ('Вернуть участок' if edit.kind == 'removed' else
                               'Отменить добавление' if edit.kind == 'added'
                               else 'Вернуть скорость')
                    restore = actions.addAction(tr(caption))
                    revise = actions.addAction(tr('Изменить границы правки'))
                    commands.extend((restore, revise))
                    restore.triggered.connect(
                        lambda checked=False, key=edit.key: picked.append((True, key)))
                    revise.triggered.connect(
                        lambda checked=False, key=edit.key: picked.append((False, key)))
                menu.exec(event.globalPosition().toPoint())
                self.edit_menu = None
                menu.deleteLater()
                if picked:
                    restore_range, key = picked[-1]
                    (self.restore_edit if restore_range else self.revise_edit).emit(key)
                return
            self.seek_at(event)
            menu = QMenu(self)
            menu.addAction(tr("Разделить здесь (Q)")).triggered.connect(self.split_scene.emit)
            menu.addAction(tr("Убрать эпизод")).triggered.connect(self.remove_scene.emit)
            menu.exec(event.globalPosition().toPoint())
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.pointer_down = True
        self.setFocus()
        self.drag_edge = ""
        self.pending_edge = ""
        x, y = event.position().x(), event.position().y()
        self.press_x = x
        self.drag_time = self.time_at(x)
        if 0 <= y <= 17:
            for edge, value in (("movie_start", 0), ("movie_end", self.maximum())):
                if abs(x-self.x_at(value)) <= 4:
                    self.movie_start, self.movie_end = 0, self.maximum()
                    self.pending_edge = edge
        elif 18 <= y <= 27:
            for row, a, b in self.blocks:
                if row == self.selected_row:
                    if self.x_at(b)-self.x_at(a) < 24:
                        break
                    distance, edge = min((abs(x-self.x_at(a)), "start"),
                                         (abs(x-self.x_at(b)), "end"))
                    if distance <= 5:
                        self.pending_edge, self.drag_row = edge, row
        if not self.pending_edge:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.pan_x is not None:
            span = self.view_span or self.maximum()
            self.set_view(self.pan_start - (event.position().x()-self.pan_x)
                          / max(1, self.width()-self.left_padding-10) * span, span)
            return
        if not self.pointer_down:
            x, y = event.position().x(), event.position().y()
            tip = self.navigation_tip
            if 27 <= y <= 55 and any(abs(x-self.x_at(ms)) <= 5
                                        for ms in self.split_positions):
                tip = tr('Ручной разрез')
            edit = self.edit_at(x, y)
            if edit:
                tip = (edit.detail or edit.label)+'\n'+tr('ПКМ — вернуть или изменить')
            handle = (0 <= y <= 17 and min(abs(x-self.x_at(0)),
                       abs(x-self.x_at(self.maximum()))) <= 4)
            if handle:
                tip = (tr("Начало ролика — потяните ручку") if abs(x-self.x_at(0)) <= 4
                       else tr("Конец ролика — потяните ручку"))
            for row, a, b in self.blocks:
                if (row == self.selected_row and 18 <= y <= 27
                        and self.x_at(b)-self.x_at(a) >= 24):
                    handle = min(abs(x-self.x_at(a)), abs(x-self.x_at(b))) <= 5
                    if handle:
                        tip = (tr("Начало эпизода — потяните ручку")
                               if abs(x-self.x_at(a)) <= 5 else
                               tr("Конец эпизода — потяните ручку"))
            if self.toolTip() != tip:
                self.setToolTip(tip)
            self.setCursor(Qt.CursorShape.SizeHorCursor if handle
                           else Qt.CursorShape.CrossCursor)
            return
        if self.pending_edge:
            if abs(event.position().x()-self.press_x) < QApplication.startDragDistance():
                return
            self.drag_edge, self.pending_edge = self.pending_edge, ""
        if not self.drag_edge:
            super().mouseMoveEvent(event)
            return
        self.drag_time = self.time_at(event.position().x())
        if self.drag_edge.startswith("movie"):
            if self.drag_edge == "movie_start":
                self.movie_start = min(self.drag_time, self.movie_end-40)
            else:
                self.movie_end = max(self.drag_time, self.movie_start+40)
            self.setValue(self.drag_time)
            self.sliderMoved.emit(self.drag_time)
        else:
            self.edge_preview.emit(self.drag_row, self.drag_edge, self.drag_time)
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            self.pan_x = None
            self.setCursor(Qt.CursorShape.CrossCursor)
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton or not self.pointer_down:
            return
        self.pointer_down = False
        self.setSliderDown(False)
        if (self.drag_edge and abs(event.position().x()-self.press_x)
                < QApplication.startDragDistance()):
            self.drag_edge = ""
            self.movie_start = self.movie_end = 0
            self.seek_at(event)
            self.update()
            self.scrub_finished.emit()
            return
        if self.pending_edge:
            self.pending_edge = ""
            self.seek_at(event)
            self.update()
            self.scrub_finished.emit()
            return
        if self.drag_edge:
            self.pointer_down = True
            self.mouseMoveEvent(event)
            self.pointer_down = False
            edge = self.drag_edge
            self.drag_edge = ""
            if edge.startswith("movie"):
                self.movie_commit.emit(self.movie_start, self.movie_end)
                self.movie_start, self.movie_end = 0, 0
                self.set_view(0, self.maximum())
            else:
                self.edge_commit.emit(self.drag_row, edge, self.drag_time)
            self.update()
        else:
            super().mouseReleaseEvent(event)
            self.seek_at(event)
        self.scrub_finished.emit()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape and self.pan_x is not None:
            self.set_view(self.pan_start, self.view_span or self.maximum())
            self.pan_x = None
            self.setCursor(Qt.CursorShape.CrossCursor)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape and self.pointer_down:
            self.pointer_down = False
            self.pending_edge = self.drag_edge = ""
            self.movie_start = self.movie_end = 0
            self.setSliderDown(False)
            self.update()
            self.scrub_finished.emit()
            event.accept()
            return
        if self.pointer_down or self.pan_x is not None:
            event.accept()
            return
        key = event.key()
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if key == Qt.Key.Key_Escape:
            event.accept()
            return
        if ctrl and key in (Qt.Key.Key_Z, Qt.Key.Key_Y):
            if not event.isAutoRepeat():
                if key == Qt.Key.Key_Y or shift:
                    self.redo_requested.emit()
                else:
                    self.undo_requested.emit()
        elif key == Qt.Key.Key_Space:
            if not event.isAutoRepeat():
                self.play_requested.emit()
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            step = (-1 if key == Qt.Key.Key_Left else 1) * (10 if shift else 1)
            self.navigate(round((round(self.value()/self.frame_ms)+step)*self.frame_ms))
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            row = max(0, min(len(self.blocks)-1,
                            self.selected_row+(-1 if key == Qt.Key.Key_Up else 1)))
            if self.blocks:
                self.navigate(self.blocks[row][1])
        elif key in (Qt.Key.Key_Home, Qt.Key.Key_End):
            self.navigate(0 if key == Qt.Key.Key_Home else self.maximum())
        elif key in (Qt.Key.Key_F, Qt.Key.Key_Backslash):
            self.reset_view()
        elif key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if not event.isAutoRepeat():
                self.remove_scene.emit()
        elif (key in (Qt.Key.Key_Q, ord('Й'))
              and event.modifiers() == Qt.KeyboardModifier.NoModifier):
            if not event.isAutoRepeat():
                self.split_scene.emit()
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def event(self, event: QEvent) -> bool:
        if (event.type() == QEvent.Type.ShortcutOverride and isinstance(event, QKeyEvent)
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier
                and event.key() in (Qt.Key.Key_Z, Qt.Key.Key_Y)):
            event.accept()
            return True
        return super().event(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self.pointer_down or self.pan_x is not None:
            event.accept()
            return
        total = max(1, self.maximum())
        old = self.view_span or total
        delta = event.pixelDelta()
        angle = event.angleDelta()
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            steps = angle.y()/120 if angle.y() else delta.y()/40
            if steps:
                span = max(min(1000, total), min(total, old * 0.7**max(-8, min(8, steps))))
                anchor = self.time_at(event.position().x())
                self.set_view(anchor-(anchor-self.view_start)/old*span, span)
        else:
            pixels = delta.x() or delta.y()
            motion = (pixels/max(1, self.width()-self.left_padding-10)*old if pixels else
                      (angle.x() or angle.y())/120*old*0.15)
            self.set_view(self.view_start-motion, old)
        event.accept()
