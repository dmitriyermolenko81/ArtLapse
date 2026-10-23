"""Compact marks for cutting the currently displayed exported movie."""

import math
from collections.abc import Callable

from PySide6.QtCore import QEvent, QPoint, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QHideEvent, QMouseEvent, QPainter, QPaintEvent, QWheelEvent
from PySide6.QtWidgets import QDoubleSpinBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from sculptcut.edit_history import EditHistory
from sculptcut.i18n import tr
from sculptcut.output_cuts import edited_time, retained_ranges, selected_ranges, source_time
from sculptcut.time_format import format_time
from sculptcut.ui_icons import ui_icon


def time_label(seconds: float, precise: bool = False) -> str:
    return format_time(seconds, 2 if precise else 0)


class CutTimeline(QWidget):
    seek_requested = Signal(int)
    view_changed = Signal(int, int)
    hover_requested = Signal(int, QPoint)
    hover_finished = Signal()

    def __init__(self, editor: "OutputCutEditor") -> None:
        super().__init__(editor)
        self.editor = editor
        self.drag = ""
        self.anchor = 0.0
        self.view_start = 0.0
        self.span = 0.0
        self.pan_x: float | None = None
        self.setFixedHeight(82)
        self.setMouseTracking(True)
        self.setToolTip(
            tr("Протяните участок для вырезки. Колесо — зум, Shift+колесо / средняя кнопка — "
            "сдвиг, двойной щелчок — весь ролик.")
        )

    def x_at(self, seconds: float) -> float:
        return 7 + (seconds - self.view_start) / max(0.001, self.span) * max(1, self.width() - 15)

    def time_at(self, x: float) -> float:
        return self.view_start + max(0.0, min(1.0, (x - 7) / max(1, self.width() - 15))) * self.span

    def set_view(self, start: float, span: float) -> None:
        duration = self.editor.edited_duration / 1e6
        self.span = min(duration, max(min(1.0, duration), span))
        self.view_start = max(0.0, min(start, duration - self.span))
        self.view_changed.emit(
            round(self.view_start * 1000), round((self.view_start + self.span) * 1000)
        )
        self.update()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self.drag or not self.span:
            event.accept()
            return
        steps = (
            event.angleDelta().y() / 120 if event.angleDelta().y() else event.pixelDelta().y() / 40
        )
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.set_view(self.view_start - steps * self.span * 0.15, self.span)
        else:
            anchor = self.time_at(event.position().x())
            ratio = (anchor - self.view_start) / self.span
            span = min(
                self.editor.edited_duration / 1e6,
                max(1.0, self.span / 1.5 ** max(-20, min(20, steps))),
            )
            self.set_view(anchor - ratio * span, span)
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag = ""
            self.set_view(0, self.editor.edited_duration / 1e6)

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#14282b"))
        p.fillRect(QRectF(7, 5, self.width() - 15, 25), QColor("#345b5a"))
        elapsed = 0
        for start, end in self.editor.ranges[:-1]:
            elapsed += end - start
            p.fillRect(QRectF(self.x_at(elapsed / 1e6) - 1, 5, 2, 25), QColor("#d5a071"))
        a, b = self.editor.start.value(), self.editor.end.value()
        left, right = self.x_at(a), self.x_at(b)
        if b > a:
            p.fillRect(QRectF(left, 5, right - left, 25), QColor("#bc7056"))
            for x in (left, right):
                p.fillRect(QRectF(x - 3, 1, 6, 33), QColor("#f2dcc6"))
        p.setPen(QColor("#dce5f3"))
        # Labels stay legible as the visible time range changes with zoom.
        target_step = self.span / max(1, (self.width() - 15) // 100)
        steps = (0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600)
        step = next((s for s in steps if s >= target_step), max(3600, target_step))
        first = math.ceil(self.view_start / step)
        last = math.floor((self.view_start + self.span) / step)
        for index in range(first, last + 1):
            seconds = index * step
            tick = round(self.x_at(seconds))
            p.setPen(QColor("#8496b0"))
            p.drawLine(tick, 30, tick, 35)
            label = time_label(seconds, precise=step < 1)
            width = p.fontMetrics().horizontalAdvance(label) + 8
            label_x = max(0.0, min(tick - width / 2, self.width() - width))
            p.setPen(QColor("#c5d2e6"))
            p.drawText(QRectF(label_x, 36, width, 18), Qt.AlignmentFlag.AlignCenter, label)
        p.setPen(QColor("#dce5f3"))
        p.drawText(
            QRectF(7, 58, self.width() - 14, 20),
            Qt.AlignmentFlag.AlignLeft,
            tr(
                f"Сейчас {time_label(self.editor.position / 1000, True)} / "
                f"{time_label(self.editor.edited_duration / 1e6, True)}"
            )
            + (
                tr(f"   ·   Выделено {time_label(a, True)} — {time_label(b, True)}")
                if b > a
                else ""
            ),
        )
        p.setPen(QColor("#8ed5ca"))
        x = round(self.x_at(self.editor.position / 1000))
        p.drawLine(x, 0, x, 30)
        p.end()

    def handle(self, x: float) -> str:
        a, b = self.x_at(self.editor.start.value()), self.x_at(self.editor.end.value())
        if self.editor.end.value() <= self.editor.start.value() or min(abs(x - a), abs(x - b)) > 7:
            return ""
        return "start" if abs(x - a) <= abs(x - b) else "end"

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self.hover_finished.emit()
        if event.button() == Qt.MouseButton.MiddleButton:
            self.pan_x = event.position().x()
            return
        if event.button() != Qt.MouseButton.LeftButton or not self.editor.duration:
            return
        x = event.position().x()
        self.drag = self.handle(x) or "new"
        self.anchor = self.time_at(x)
        if self.drag == "new":
            self.editor.start.setValue(self.anchor)
            self.editor.end.setValue(self.anchor)
        self.seek_requested.emit(round(self.anchor * 1000))

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        x = event.position().x()
        self.setToolTip(
            time_label(self.time_at(x), True) + tr(" · Колесо — зум; Shift+колесо — сдвиг")
        )
        if self.pan_x is not None:
            self.set_view(
                self.view_start - (x - self.pan_x) * self.span / max(1, self.width() - 15),
                self.span,
            )
            self.pan_x = x
            return
        if not self.drag:
            self.hover_requested.emit(
                round(self.time_at(x) * 1000), self.mapToGlobal(QPoint(round(x), 0))
            )
            self.setCursor(
                Qt.CursorShape.SizeHorCursor if self.handle(x) else Qt.CursorShape.CrossCursor
            )
            return
        seconds = self.time_at(x)
        if self.drag == "start":
            seconds = min(seconds, max(0, self.editor.end.value() - 0.01))
            self.editor.start.setValue(seconds)
        elif self.drag == "end":
            seconds = max(seconds, self.editor.start.value() + 0.01)
            self.editor.end.setValue(seconds)
        else:
            self.editor.start.setValue(min(self.anchor, seconds))
            self.editor.end.setValue(max(self.anchor, seconds))
        self.seek_requested.emit(round(seconds * 1000))
        self.update()

    def leaveEvent(self, event: QEvent) -> None:
        self.hover_finished.emit()
        super().leaveEvent(event)

    def hideEvent(self, event: QHideEvent) -> None:
        self.hover_finished.emit()
        super().hideEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            self.pan_x = None
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag = ""


class OutputCutEditor(QWidget):
    save_requested = Signal()
    changed = Signal()

    def __init__(self, history: EditHistory | None = None) -> None:
        super().__init__()
        self.setObjectName("outputCutPanel")
        self.shared_history = history is not None
        self.position = 0
        self.duration = 0
        self.cuts: list[tuple[int, int]] = []
        self.history = history if history is not None else EditHistory()
        self.record_cuts: Callable[[list[tuple[int, int]], list[tuple[int, int]]], None] = (
            lambda before, after: self.history.record(tr("Вырезка"), before, after, self.set_cuts)
        )
        self.undo_handler: Callable[[], None] = self.history.undo
        self.redo_handler: Callable[[], None] = self.history.redo
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 10)
        layout.setSpacing(6)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.start, self.end = QDoubleSpinBox(), QDoubleSpinBox()
        for name, field in ((tr("Начало"), self.start), (tr("Конец"), self.end)):
            field.setDecimals(2)
            field.setSuffix(tr(" с"))
            field.setSingleStep(0.1)
            field.setAccessibleName(name + tr(" удаляемого участка"))
            field.setParent(self)
            field.hide()
        self.timeline = CutTimeline(self)
        self.start.valueChanged.connect(self.timeline.update)
        self.end.valueChanged.connect(self.timeline.update)
        layout.addWidget(self.timeline)
        self.remove_button = QPushButton(tr("Вырезать участок"))
        self.remove_button.setProperty("actionRole", "secondary")
        self.remove_button.setIcon(ui_icon("cut", "#b5d6cd"))
        self.remove_button.setToolTip(tr("Удалить выделенный участок, сохранив остальной видеоряд"))
        self.remove_button.clicked.connect(self.add_cut)
        row.addWidget(self.remove_button)
        actions = QHBoxLayout()
        self.summary = QLabel(tr("Укажите начало и конец по ползунку просмотра."))
        self.summary.setObjectName("cutSummary")
        self.summary.setWordWrap(True)
        row.addWidget(self.summary, 1)
        layout.addLayout(row)
        self.undo_button = QPushButton(tr("Отменить"))
        self.undo_button.setProperty("actionRole", "utility")
        self.undo_button.setToolTip(tr("Вернуть последний вырезанный участок"))
        self.undo_button.setIcon(ui_icon("undo", "#a4c4bb"))
        self.undo_button.clicked.connect(self.undo)
        actions.addWidget(self.undo_button)
        self.redo_button = QPushButton(tr("Повторить"))
        self.redo_button.setProperty("actionRole", "utility")
        self.redo_button.setIcon(ui_icon("redo", "#a4c4bb"))
        self.redo_button.clicked.connect(self.redo)
        actions.addWidget(self.redo_button)
        actions.addStretch(1)
        self.save_button = QPushButton(tr("Сохранить копию"))
        self.save_button.setObjectName("primary")
        self.save_button.setToolTip(tr("Выбрать способ подрезки и сохранить в новый файл"))
        self.save_button.clicked.connect(self.save_requested.emit)
        actions.addWidget(self.save_button)
        layout.addLayout(actions)
        layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetMinimumSize)
        self.start.valueChanged.connect(self.update_actions)
        self.end.valueChanged.connect(self.update_actions)
        self.reset()
        self.hide()

    def reset(self) -> None:
        self.cuts.clear()
        if not self.shared_history:
            self.history.clear()
        self.start.setValue(0)
        self.end.setValue(0)
        self.timeline.set_view(0, self.edited_duration / 1e6)
        self.refresh()

    def set_duration(self, milliseconds: int) -> None:
        old = self.duration
        self.duration = milliseconds * 1000
        for field in (self.start, self.end):
            field.setMaximum(self.edited_duration / 1e6)
        if old != self.duration:
            self.timeline.set_view(0, self.edited_duration / 1e6)
        self.refresh()

    @property
    def ranges(self) -> list[tuple[int, int]]:
        return retained_ranges(self.duration, self.cuts) if self.duration else []

    @property
    def edited_duration(self) -> int:
        return sum(b - a for a, b in self.ranges)

    def to_source(self, milliseconds: int) -> int:
        return source_time(self.ranges, milliseconds * 1000) // 1000

    def to_edited(self, milliseconds: int) -> int:
        return edited_time(self.ranges, milliseconds * 1000) // 1000

    def set_position(self, milliseconds: int) -> None:
        self.position = self.to_edited(milliseconds)
        self.timeline.update()

    def add_cut(self) -> None:
        try:
            selected = selected_ranges(
                self.ranges, round(self.start.value() * 1e6), round(self.end.value() * 1e6)
            )
            proposed = self.cuts + selected
            retained_ranges(self.duration, proposed)
            self.record_cuts(list(self.cuts), list(proposed))
        except (ValueError, OSError) as exc:
            self.summary.setText(str(exc))
            return
        self.cuts = proposed
        self.apply_change()

    def undo(self) -> None:
        self.undo_handler()
        self.update_actions()

    def redo(self) -> None:
        self.redo_handler()
        self.update_actions()

    def set_cuts(self, cuts: list[tuple[int, int]]) -> None:
        self.cuts = list(cuts)
        self.apply_change()

    def apply_change(self) -> None:
        self.start.setValue(0)
        self.end.setValue(0)
        for field in (self.start, self.end):
            field.setMaximum(self.edited_duration / 1e6)
        self.timeline.set_view(self.timeline.view_start, self.timeline.span)
        self.refresh()
        self.changed.emit()

    def refresh(self) -> None:
        self.timeline.update()
        self.save_button.setEnabled(bool(self.cuts))
        self.update_actions()
        self.summary.setText(
            tr(f"Удалено: {time_label((self.duration - self.edited_duration) / 1e6, True)} · "
            f"Ролик: {time_label(self.edited_duration / 1e6, True)}")
            if self.cuts
            else tr("Выделите участок на шкале.")
        )

    def update_actions(self) -> None:
        selection = self.end.value() - self.start.value()
        self.remove_button.setEnabled(0 < selection < self.edited_duration / 1e6)
        self.undo_button.setEnabled(bool(self.history.undo_label))
        self.redo_button.setEnabled(bool(self.history.redo_label))
