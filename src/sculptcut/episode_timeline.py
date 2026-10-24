"""Source-time episode bands and non-destructive trim handles."""

from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QRectF, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent, QWheelEvent
from PySide6.QtWidgets import QWidget

from sculptcut.domain import Project
from sculptcut.episode_editor import EpisodeEditor, edit_episode
from sculptcut.i18n import tr
from sculptcut.planning import RenderDecision
from sculptcut.project import load_project
from sculptcut.time_format import format_time


def trim_episode(
    project: Project, decision: RenderDecision, start: int, end: int, fps: int
) -> Project:
    source = next(s for s in project.sources if s.id == decision.source_id)
    if not 0 <= start < end <= source.duration_us:
        raise ValueError(tr("Границы эпизода вне исходного видео"))
    if decision.action == "DROP":
        raise ValueError(tr("Сначала выберите «Оставить» для пропущенного участка"))
    speed = max(
        1.0,
        (decision.source_end_us - decision.source_start_us)
        * fps
        / (1_000_000 * max(1, decision.output_frame_count)),
    )
    union = decision.model_copy(
        update={
            "source_start_us": min(start, decision.source_start_us),
            "source_end_us": max(end, decision.source_end_us),
        }
    )
    project = edit_episode(project, union, None)
    return edit_episode(
        project,
        decision.model_copy(
            update={
                "source_start_us": start,
                "source_end_us": end,
            }
        ),
        speed,
    )


def clock(us: int) -> str:
    return format_time(us // 1_000_000)


class EpisodeTimeline(QWidget):
    hover_requested = Signal(int, QPoint)
    hover_finished = Signal()
    dragging = Signal(bool)
    seek_requested = Signal(int)
    view_changed = Signal(int, int)

    def __init__(self, editor: EpisodeEditor) -> None:
        super().__init__()
        self.editor = editor
        self.url = QUrl()
        self.source_id = ""
        self.duration = 0
        self.view_start = 0
        self.view_end = 0
        self.pan_x: float | None = None
        self.position = 0
        self.selected = -1
        self.drag = ""
        self.scrubbing = False
        self.bounds = (0, 0)
        self.setFixedHeight(54)
        self.setMouseTracking(True)
        self.setToolTip(
            tr("Нажмите участок для просмотра. ПКМ — действия. "
            "Тяните границы выбранного эпизода для изменения длительности.")
        )
        editor.loaded.connect(self.refresh)
        editor.changed.connect(self.refresh_after_edit)
        self.hide()

    def refresh_after_edit(self, message: str) -> None:
        if self.editor.root:
            item = self.editor.currentItem()
            edited: RenderDecision | None = (item.data(Qt.ItemDataRole.UserRole)
                                            if item is not None else None)
            self.editor.load(self.editor.root)
            if edited is not None and not edited.output_frame_count:
                # Reload can merge adjacent omissions and change every later row.
                # Anchor to source time, not the old player position or row index.
                kept = [(i, d) for i, d in self.visible_decisions()
                        if d.action != "DROP" and d.source_id == edited.source_id]
                previous = [(i, d) for i, d in kept
                            if d.source_end_us <= edited.source_start_us]
                following = [(i, d) for i, d in kept
                             if d.source_start_us >= edited.source_end_us]
                target = previous[-1] if previous else following[0] if following else None
                if target is not None:
                    self.selected, decision = target
                    item = self.editor.item(self.selected)
                    self.editor.setCurrentItem(item)
                    self.editor.scrollToItem(item)
                    self.set_position(decision.source_start_us // 1000)
                    self.editor.preview_episode(item)

    def refresh(self) -> None:
        if not getattr(self.editor, "show_cards", False):
            self.editor.hide()
        self.set_source(self.url)

    def set_source(self, url: QUrl) -> None:
        previous = (self.url, self.source_id, self.duration)
        self.url = url
        self.source_id, self.duration = "", 0
        if self.editor.root and self.editor.plan:
            try:
                project = load_project(self.editor.root)
                source = next(
                    (
                        s
                        for s in project.sources
                        if s.path.resolve() == Path(url.toLocalFile()).resolve()
                    ),
                    None,
                )
                if source:
                    self.source_id, self.duration = source.id, source.duration_us
            except (OSError, ValueError) as exc:
                self.editor.failed.emit(str(exc))
        self.selected = -1
        if previous != (url, self.source_id, self.duration):
            self.set_view(0, self.duration)
        self.setVisible(self.duration > 0)
        self.update()

    def decisions(self) -> list[tuple[int, RenderDecision]]:
        return [
            (i, self.editor.item(i).data(Qt.ItemDataRole.UserRole))
            for i in range(self.editor.count())
            if self.editor.item(i).data(Qt.ItemDataRole.UserRole).source_id == self.source_id
        ]

    def x_at(self, time: int) -> float:
        return 7 + (time - self.view_start) / max(1, self.view_end - self.view_start) * max(
            1, self.width() - 15
        )

    def visible_decisions(self) -> list[tuple[int, RenderDecision]]:
        return [(i, d) for i, d in self.decisions() if not self.editor.isRowHidden(i)]

    def time_at(self, x: float) -> int:
        ratio = max(0.0, min(1.0, (x - 7) / max(1, self.width() - 15)))
        return self.view_start + round(ratio * (self.view_end - self.view_start))

    def set_view(self, start: int, span: int) -> None:
        span = min(self.duration, max(min(1_000_000, self.duration), span))
        self.view_start = max(0, min(start, self.duration - span))
        self.view_end = self.view_start + span
        self.view_changed.emit(self.view_start // 1000, self.view_end // 1000)
        self.update()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if not self.duration or self.drag:
            event.accept()
            return
        delta = event.angleDelta().y() or event.angleDelta().x()
        steps = delta / 120 if delta else event.pixelDelta().y() / 40
        span = self.view_end - self.view_start
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.set_view(self.view_start - round(steps * span * 0.15), span)
        else:
            anchor = self.time_at(event.position().x())
            ratio = (anchor - self.view_start) / max(1, span)
            new_span = min(
                self.duration,
                max(min(1_000_000, self.duration), round(span / 1.5 ** max(-20, min(20, steps)))),
            )
            self.set_view(round(anchor - ratio * new_span), new_span)
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and not self.drag:
            self.set_view(0, self.duration)
            event.accept()

    def set_position(self, milliseconds: int) -> None:
        self.position = milliseconds * 1000
        if not self.drag:
            self.selected = next(
                (
                    i
                    for i, d in self.visible_decisions()
                    if d.source_start_us <= self.position < d.source_end_us
                ),
                -1,
            )
        self.update()

    def selected_decision(self) -> RenderDecision | None:
        return next((d for i, d in self.visible_decisions() if i == self.selected), None)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#181d26"))
        for i, band in self.visible_decisions():
            start, end = (
                self.bounds
                if self.drag and i == self.selected
                else (band.source_start_us, band.source_end_us)
            )
            left, right = self.x_at(start), self.x_at(end)
            if right < 7 or left > self.width() - 8:
                continue
            left, right = max(7.0, left), min(float(self.width() - 8), right)
            rect = QRectF(left, 5, max(1.0, right - left), 26)
            painter.fillRect(rect, QColor("#303642" if band.action == "DROP" else "#4269a5"))
            painter.setPen(QColor("#13161d"))
            painter.drawRect(rect)
            if right - left > 65:
                painter.setPen(QColor("#d5ddeb"))
                painter.drawText(
                    rect,
                    Qt.AlignmentFlag.AlignCenter,
                    tr("Пропуск") if band.action == "DROP" else tr("В ролике"),
                )
        d = self.selected_decision()
        if d:
            start, end = self.bounds if self.drag else (d.source_start_us, d.source_end_us)
            if d.action != "DROP":
                for time in (start, end):
                    painter.fillRect(QRectF(self.x_at(time) - 3, 2, 6, 32), QColor("#bcd1ff"))
            painter.setPen(QColor("#bcd1ff"))
            text = f"{clock(start)} — {clock(end)} · {format_time((end - start) / 1e6, 1)}"
            if not self.drag and d.action != "DROP" and self.editor.plan is not None:
                output = d.output_frame_count / self.editor.plan.output_fps
                text += f" → {format_time(output, 1)}"
                if d.action in ("SPEED", "KEEP") and output > 0:
                    text += f" · {(end - start) / 1e6 / output:.1f}×"
        else:
            painter.setPen(QColor("#9aa8be"))
            text = ""
        painter.drawText(QRectF(7, 36, self.width() - 14, 18), Qt.AlignmentFlag.AlignLeft, text)
        painter.setPen(QColor("#18a7f5"))
        x = round(self.x_at(self.position))
        painter.drawLine(x, 0, x, 34)
        painter.end()

    def handle_at(self, x: float) -> str:
        d = self.selected_decision()
        if (not d or d.action not in ("SPEED", "KEEP") or d.sample_times_us):
            return ""
        a, b = self.x_at(d.source_start_us), self.x_at(d.source_end_us)
        if min(abs(x - a), abs(x - b)) > 7:
            return ""
        return "start" if abs(x - a) <= abs(x - b) else "end"

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            self.pan_x = event.position().x()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        if not self.isEnabled() or event.position().y() > 35:
            return
        x = event.position().x()
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag = self.handle_at(x)
            d = self.selected_decision()
            if self.drag and d:
                self.bounds = d.source_start_us, d.source_end_us
                self.dragging.emit(True)
                return
        time = self.time_at(x)
        candidates = [
            (i, d) for i, d in self.visible_decisions()
            if d.source_start_us <= time < d.source_end_us
        ]
        if not candidates:
            return
        self.selected, d = candidates[0]
        item = self.editor.item(self.selected)
        self.editor.setCurrentItem(item)
        if event.button() == Qt.MouseButton.RightButton:
            self.editor.choose(item)
        else:
            self.scrubbing = True
            self.editor.preview_requested.emit(
                self.url.toLocalFile(), time // 1000, d.source_end_us // 1000
            )
            self.seek_requested.emit(min(time // 1000, max(0, self.duration // 1000 - 1)))
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.scrubbing:
            time = self.time_at(event.position().x())
            self.seek_requested.emit(min(time // 1000, max(0, self.duration // 1000 - 1)))
            return
        if self.pan_x is not None:
            x = event.position().x()
            span = self.view_end - self.view_start
            self.set_view(
                self.view_start - round((x - self.pan_x) * span / max(1, self.width() - 15)), span
            )
            self.pan_x = x
            return
        d = self.selected_decision()
        if not self.drag or d is None:
            self.setCursor(
                Qt.CursorShape.SizeHorCursor
                if self.handle_at(event.position().x())
                else Qt.CursorShape.PointingHandCursor
            )
            time = self.time_at(event.position().x())
            self.setToolTip(
                clock(time) + tr(" · Колесо — зум; Shift+колесо — сдвиг; "
                "двойной щелчок — вся запись; ПКМ — действия")
            )
            return
        lower, upper = 0, self.duration
        for i, other in self.decisions():
            if i == self.selected or other.action == "DROP":
                continue
            if other.source_end_us <= d.source_start_us:
                lower = max(lower, other.source_end_us)
            if other.source_start_us >= d.source_end_us:
                upper = min(upper, other.source_start_us)
        start, end = self.bounds
        time = self.time_at(event.position().x())
        if self.drag == "start":
            start = max(lower, min(end - 100_000, time))
        else:
            end = min(upper, max(start + 100_000, time))
        self.bounds = start, end
        boundary = start if self.drag == "start" else max(start, end - 1)
        milliseconds = min(max(0, self.duration // 1000 - 1), boundary // 1000)
        self.seek_requested.emit(milliseconds)
        self.hover_requested.emit(milliseconds, self.mapToGlobal(
            QPoint(round(self.x_at(boundary)), 0)
        ))
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.scrubbing = False
        if event.button() == Qt.MouseButton.MiddleButton:
            self.pan_x = None
            self.unsetCursor()
            return
        d = self.selected_decision()
        if not self.drag or d is None or not self.editor.root or not self.editor.plan:
            return
        self.drag = ""
        self.hover_finished.emit()
        self.dragging.emit(False)
        start, end = self.bounds
        if (start, end) == (d.source_start_us, d.source_end_us):
            return
        try:
            before = load_project(self.editor.root)
            project = trim_episode(before, d, start, end, self.editor.plan.output_fps)
            self.editor.save_edit(before, project, tr("Границы эпизода"))
            self.editor.changed.emit(tr("Границы сохранены. Экспорт применит изменения."))
            self.selected = next(
                (
                    i
                    for i, item in self.decisions()
                    if item.action != "DROP" and item.source_start_us == start
                ),
                -1,
            )
        except (OSError, ValueError) as exc:
            self.editor.failed.emit(str(exc))
        self.update()

    def leaveEvent(self, event: QEvent) -> None:
        if not self.drag:
            self.hover_finished.emit()
        super().leaveEvent(event)
