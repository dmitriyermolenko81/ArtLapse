"""Route editing keys to the visible lane under the mouse without moving focus."""
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QObject, QPoint, Qt
from PySide6.QtGui import QEnterEvent, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QLineEdit,
    QPlainTextEdit,
    QTextEdit,
    QWidget,
)

from sculptcut.simple_timeline import SimpleTimeline

if TYPE_CHECKING:
    from sculptcut.source_selection_dialog import SourceSelectionDialog


class HoverTimelineKeys(QObject):
    def __init__(self, owner: 'SourceSelectionDialog') -> None:
        super().__init__(owner)
        self.owner = owner
        assert isinstance(owner.montage_seek, SimpleTimeline)
        self.video_lane = owner.montage_seek
        self.pointer: QPoint | None = None
        app = QApplication.instance()
        assert isinstance(app, QApplication)
        self.app = app
        app.installEventFilter(self)

    def shutdown(self) -> None:
        self.app.removeEventFilter(self)

    @staticmethod
    def command(event: QKeyEvent) -> str:
        key, mods = event.key(), event.modifiers()
        if mods == Qt.KeyboardModifier.NoModifier:
            if key in (Qt.Key.Key_Q, ord('Й')):
                return 'split'
            if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
                return 'delete'
        if mods in (Qt.KeyboardModifier.ControlModifier,
                    Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
            if key in (Qt.Key.Key_Z, Qt.Key.Key_Y):
                return 'history'
        return ''

    def target(self) -> SimpleTimeline | None:
        owner = self.owner
        music = owner.music_track
        if (owner.closed or owner.loading or not owner.isVisible() or not owner.isEnabled()
                or music is None or self.app.activePopupWidget() is not None
                or self.app.activeModalWidget() is not None):
            return None
        focus = self.app.focusWidget()
        if isinstance(focus, (QLineEdit, QAbstractSpinBox, QTextEdit, QPlainTextEdit)):
            return None
        if self.pointer is None:
            return None
        window = owner.window()
        widget = window.childAt(window.mapFromGlobal(self.pointer))
        for lane in (self.video_lane, music.lane):
            if (isinstance(lane, SimpleTimeline) and lane.isVisible() and lane.isEnabled()
                    and (widget is lane or widget is not None and lane.isAncestorOf(widget))):
                point = lane.mapFromGlobal(self.pointer)
                if lane.left_padding <= point.x() <= lane.width()-10:
                    return lane
        return None

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if isinstance(watched, QWidget) and watched.window() is self.owner.window():
            if isinstance(event, (QMouseEvent, QEnterEvent)) and event.type() in (
                    QEvent.Type.MouseMove, QEvent.Type.Enter):
                self.pointer = watched.mapToGlobal(event.position().toPoint())
            elif event.type() == QEvent.Type.Leave and watched in (
                    self.video_lane,
                    self.owner.music_track.lane if self.owner.music_track else None):
                self.pointer = None
        if (event.type() not in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress)
                or not isinstance(event, QKeyEvent) or not isinstance(watched, QWidget)
                or watched.window() is not self.owner.window()):
            return False
        if event.key() == Qt.Key.Key_Space and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            owner = self.owner
            focus = self.app.focusWidget()
            if (owner.closed or owner.loading or not owner.isVisible() or not owner.isEnabled()
                    or self.app.activePopupWidget() is not None
                    or self.app.activeModalWidget() is not None
                    or isinstance(focus,
                        (QAbstractSpinBox, QTextEdit, QPlainTextEdit))
                    or (isinstance(focus, QLineEdit)
                        and not focus.isReadOnly())):
                return False
            event.accept()
            if event.type() == QEvent.Type.KeyPress and not event.isAutoRepeat():
                music = owner.music_track
                if not (self.video_lane.pointer_down or self.video_lane.pan_x is not None) and not (
                        music and (music.lane.pointer_down or music.lane.pan_x is not None)):
                    owner.play_montage()
            return True
        command = self.command(event)
        lane = self.target() if command else None
        if lane is None:
            return False
        event.accept()
        if event.type() == QEvent.Type.ShortcutOverride or event.isAutoRepeat():
            return True
        music = self.owner.music_track
        assert music is not None
        if lane is music.lane and music.locked:
            return True
        if any(track.pointer_down or track.pan_x is not None
               for track in (self.video_lane, music.lane)):
            return True
        if command in ('split', 'delete'):
            assert self.pointer is not None
            ms = lane.time_at(lane.mapFromGlobal(self.pointer).x())
            row = next((row for row, a, b in lane.blocks if a <= ms < b), -1)
            if row < 0:
                return True
            self.owner.seek_montage(ms, follow=False)
            if lane is music.lane:
                music.select(row)
        lane.keyPressEvent(event)
        return True
