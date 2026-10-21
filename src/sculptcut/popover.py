"""Small dialog panels anchored to toolbar buttons without window chrome."""

from PySide6.QtCore import QEvent, QEventLoop, QObject, QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QKeyEvent, QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QApplication, QDialog, QWidget


class AnchoredPanel(QObject):
    """A child panel with bounded placement and dismissal that consumes outside clicks."""

    def __init__(self, dialog: QDialog, anchor: QWidget) -> None:
        super().__init__(dialog)
        self.dialog = dialog
        self.anchor = anchor
        self.anchor_style = anchor.styleSheet()
        self.owner = anchor.window()
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.place)
        dialog.setParent(self.owner, Qt.WindowType.Widget)
        dialog.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        dialog.setProperty("anchoredPanel", True)
        dialog.setAccessibleName(dialog.windowTitle())
        anchor.setProperty("panelOpen", True)
        anchor.setStyleSheet(self.anchor_style + """
            QPushButton[panelOpen="true"] { background:#213f60;
                border:1px solid #76b4f4; border-radius:8px; }
        """)
        header = dialog.findChild(QWidget, "dialogHeader")
        if header is not None:
            header.hide()
        dialog.setStyleSheet(dialog.styleSheet() + """
            QDialog[anchoredPanel="true"] { background:#101318;
                border:1px solid #5c7592; border-radius:12px; }
        """)
        self.app = QApplication.instance()
        if self.app is not None:
            self.app.installEventFilter(self)

    def place(self) -> None:
        self.dialog.adjustSize()
        bounds = self.owner.rect().adjusted(8, 8, -8, -8)
        button = QRect(self.anchor.mapTo(self.owner, QPoint()), self.anchor.size())
        width, height = self.dialog.width(), self.dialog.height()
        x = button.left()
        if x + width > bounds.right() + 1:
            x = button.right() + 1 - width
        y = button.bottom() + 9
        if y + height > bounds.bottom() + 1:
            y = button.top() - height - 8
        x = max(bounds.left(), min(x, bounds.right() + 1 - width))
        y = max(bounds.top(), min(y, bounds.bottom() + 1 - height))
        self.dialog.move(x, y)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched in (self.owner, self.anchor, self.dialog) and event.type() in (
            QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.LayoutRequest,
        ):
            self.timer.start(0)
        elif watched is self.owner and event.type() in (QEvent.Type.Close, QEvent.Type.Hide):
            self.dialog.reject()
        if not self.dialog.isVisible():
            return False
        if self.dialog.property("nativeDialogOpen"):
            return False
        modal = QApplication.activeModalWidget()
        if modal is not None and self.owns(modal):
            return False
        if event.type() == QEvent.Type.ApplicationDeactivate:
            self.dialog.reject()
        elif isinstance(event, QMouseEvent) and event.type() == QEvent.Type.MouseButtonPress:
            popup = QApplication.activePopupWidget()
            point = event.globalPosition().toPoint()
            in_panel = self.dialog.rect().contains(self.dialog.mapFromGlobal(point))
            in_popup = (popup is not None and self.owns(popup)
                        and popup.rect().contains(popup.mapFromGlobal(point)))
            if not in_panel and not in_popup:
                self.dialog.reject()
                return True
        elif isinstance(event, QKeyEvent) and event.type() == QEvent.Type.KeyPress:
            popup = QApplication.activePopupWidget()
            if event.key() == Qt.Key.Key_Escape and popup is None:
                self.dialog.reject()
                return True
            if event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab) and popup is None:
                self.focus_step(event.key() == Qt.Key.Key_Backtab
                                or bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
                return True
        elif isinstance(event, QWheelEvent) and not self.owns(watched):
            if not self.dialog.rect().contains(
                self.dialog.mapFromGlobal(event.globalPosition().toPoint()),
            ):
                return True
        elif event.type() == QEvent.Type.Shortcut and not self.owns(watched):
            return True
        return False

    def focus_step(self, backwards: bool = False) -> None:
        focused = QApplication.focusWidget()
        current: QWidget = focused if focused is not None and self.owns(focused) else self.dialog
        visited: set[QWidget] = set()
        while current not in visited:
            visited.add(current)
            following = current.previousInFocusChain() if backwards else current.nextInFocusChain()
            if following is None:
                return
            current = following
            if (self.owns(current) and current.isVisible() and current.isEnabled()
                    and current.focusPolicy() & Qt.FocusPolicy.TabFocus):
                current.setFocus(Qt.FocusReason.BacktabFocusReason if backwards
                                 else Qt.FocusReason.TabFocusReason)
                return

    def owns(self, obj: QObject) -> bool:
        current: QObject | None = obj
        while current is not None:
            if current is self.dialog:
                return True
            current = current.parent()
        return False

    def detach(self) -> None:
        self.timer.stop()
        if self.app is not None:
            self.app.removeEventFilter(self)
        self.anchor.setProperty("panelOpen", False)
        self.anchor.setStyleSheet(self.anchor_style)
        self.deleteLater()


def exec_anchored(dialog: QDialog, anchor: QWidget) -> int:
    placement = AnchoredPanel(dialog, anchor)
    loop = QEventLoop(dialog)
    dialog.finished.connect(loop.quit)
    try:
        placement.place()
        dialog.show()
        dialog.raise_()
        dialog.setFocus(Qt.FocusReason.PopupFocusReason)
        placement.focus_step()
        if dialog.isVisible():
            loop.exec()
        return dialog.result()
    finally:
        dialog.finished.disconnect(loop.quit)
        loop.deleteLater()
        placement.detach()
        if anchor.isVisible():
            anchor.setFocus(Qt.FocusReason.PopupFocusReason)
