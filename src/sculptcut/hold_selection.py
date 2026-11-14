"""Long-press range selection for actionable lists and compact popup lists."""
from PySide6.QtCore import (
    QEvent,
    QItemSelection,
    QItemSelectionModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QPoint,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QKeyEvent, QMouseEvent, QPaintEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFrame,
    QListView,
    QStyle,
    QStyleOptionComboBox,
    QStylePainter,
    QVBoxLayout,
)
from shiboken6 import isValid

from sculptcut.i18n import tr


class HoldSelection(QObject):
    changed = Signal()
    cancelled = Signal()

    def __init__(self, view: QAbstractItemView,
                 group_role: Qt.ItemDataRole | None = None, *, popup: bool = False) -> None:
        super().__init__(view)
        self.view = view
        self._viewport = view.viewport()
        self.group_role = group_role
        self.popup = popup
        self.active = False
        self.dragging = False
        self.anchor = -1
        self.point = QPoint()
        self.press = QPoint()
        self.base: set[int] = set()
        self.selecting = True
        self._gesture_window: QObject | None = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.activate)
        self.scroll = QTimer(self)
        self.scroll.setInterval(50)
        self.scroll.timeout.connect(self.auto_scroll)
        view.viewport().installEventFilter(self)
        view.installEventFilter(self)
        view.selectionModel().selectionChanged.connect(lambda *_: self.changed.emit())
        view.model().modelReset.connect(self.reset)
        view.model().rowsRemoved.connect(lambda *_: self.reset())
        view.destroyed.connect(self.stop)

    def rows(self) -> set[int]:
        return {i.row() for i in self.view.selectionModel().selectedRows()}

    def replace(self, rows: set[int]) -> None:
        selection = QItemSelection()
        model = self.view.model()
        for row in sorted(rows):
            index = model.index(row, 0)
            if index.isValid():
                selection.select(index, index)
        self.view.selectionModel().select(selection,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows)

    def stop(self) -> None:
        self.timer.stop()
        self.scroll.stop()
        self.dragging = False
        self.anchor = -1
        if self._gesture_window is not None and isValid(self._gesture_window):
            self._gesture_window.removeEventFilter(self)
        self._gesture_window = None

    def reset(self) -> None:
        self.stop()
        self.active = False
        self.changed.emit()

    def activate(self) -> None:
        if self.anchor < 0 or not self.view.isVisible() or not self.view.isEnabled():
            return
        self.active = self.dragging = True
        self.update_range()
        self.changed.emit()

    def update_range(self) -> None:
        viewport = self.view.viewport()
        point = QPoint(max(1, min(viewport.width()-2, self.point.x())),
                       max(1, min(viewport.height()-2, self.point.y())))
        index = self.view.indexAt(point)
        row = index.row() if index.isValid() else self.anchor
        if row < 0 or self.anchor < 0:
            return
        region = set(range(min(row, self.anchor), max(row, self.anchor)+1))
        region = {i for i in region if not self.view.isIndexHidden(
            self.view.model().index(i, 0))}
        if self.group_role is not None:
            model = self.view.model()
            values = {model.index(i, 0).data(self.group_role) for i in region}
            region = {i for i in range(model.rowCount())
                      if model.index(i, 0).data(self.group_role) in values}
        self.replace(self.base | region if self.selecting else self.base-region)

    def auto_scroll(self) -> None:
        if not self.dragging:
            return
        margin = self.view.autoScrollMargin()
        horizontal = (self.view.horizontalScrollBar().maximum() > 0
                      and self.view.verticalScrollBar().maximum() == 0)
        bar = (self.view.horizontalScrollBar() if horizontal else
               self.view.verticalScrollBar())
        point = self.point.x() if horizontal else self.point.y()
        limit = self.view.viewport().width() if horizontal else self.view.viewport().height()
        direction = -1 if point < margin else (1 if point > limit-margin else 0)
        if direction:
            bar.setValue(bar.value()+direction*bar.singleStep())
            self.update_range()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if not isValid(self.view):
            return False
        if isinstance(event, QKeyEvent) and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape and self.active:
                self.reset()
                self.replace(set())
                self.cancelled.emit()
                return True
        if event.type() in (QEvent.Type.Hide, QEvent.Type.WindowDeactivate):
            self.stop()
        if not isinstance(event, QMouseEvent) or watched is not self._viewport:
            return False
        if event.type() == QEvent.Type.MouseButtonPress:
            if event.button() != Qt.MouseButton.LeftButton or event.modifiers():
                return False
            index = self.view.indexAt(event.position().toPoint())
            if not index.isValid():
                return False
            self.base = self.rows() if self.active else set()
            self.selecting = not self.active or index.row() not in self.base
            window = self.view.window()
            if window is not self.view:
                self._gesture_window = window
                window.installEventFilter(self)
            self.anchor = index.row()
            self.point = self.press = event.position().toPoint()
            self.timer.start(QApplication.startDragTime())
            return self.active
        if (event.type() == QEvent.Type.MouseMove and self.active
                and self.popup and self.anchor < 0):
            return True  # Popup hover must not replace the marked group with one row.
        if event.type() == QEvent.Type.MouseMove and self.anchor >= 0:
            self.point = event.position().toPoint()
            if self.dragging:
                self.update_range()
                self.scroll.start()
                return True
            if (self.point-self.press).manhattanLength() >= QApplication.startDragDistance():
                self.timer.stop()
                if not self.active:
                    self.anchor = -1
        if event.type() == QEvent.Type.MouseButtonRelease:
            if event.button() == Qt.MouseButton.LeftButton and self.anchor >= 0:
                consumed = self.active
                self.point = event.position().toPoint()
                if self.active:
                    self.update_range()
                self.stop()
                return consumed
        return False


class SelectionCombo(QComboBox):
    """Combo caption with an independent popup view supporting real multi-selection."""
    bulk_selection_changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._marked: list[QPersistentModelIndex] = []
        self._changing_popup = False
        self._popup = QFrame(self, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self._popup.setObjectName('selectionPopup')
        self._popup.setStyleSheet('QFrame#selectionPopup {background:#171d28; '
                                  'border:1px solid #52617a;}')
        layout = QVBoxLayout(self._popup)
        layout.setContentsMargins(1, 1, 1, 1)
        self._list = QListView(self._popup)
        self._list.setObjectName('selectionList')
        self._list.setStyleSheet('''
            QListView#selectionList { background:#171d28; color:#e9edf5;
                border:0; outline:0; padding:3px; }
            QListView#selectionList::item { padding:2px 6px;
                border:1px solid transparent; border-radius:3px; }
            QListView#selectionList::item:hover { background:#252d3b; }
            QListView#selectionList::item:selected { background:#314b77;
                border-color:#6b94df; color:#fff; }
        ''')
        self._list.setModel(self.model())
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._list.setUniformItemSizes(True)
        layout.addWidget(self._list)
        self.hold = HoldSelection(self._list, popup=True)
        self._list.installEventFilter(self)
        self._list.clicked.connect(self.choose_index)
        self.hold.changed.connect(self.capture_selection)
        self.hold.cancelled.connect(self.hidePopup)
        self.activated.connect(lambda _: self.hold.reset())
        self.currentIndexChanged.connect(lambda _: self.hold.reset())
        self.setAccessibleDescription(tr(
            'Удерживайте ЛКМ и ведите по списку для выбора нескольких.'))

    def view(self) -> QAbstractItemView:
        return self._list if hasattr(self, '_list') else super().view()

    def selected_rows(self) -> list[int]:
        rows = sorted(i.row() for i in self._marked if i.isValid())
        if self.hold.active:
            return rows
        return [self.currentIndex()] if self.currentIndex() >= 0 else []

    def capture_selection(self) -> None:
        if (not isValid(self._list) or self._changing_popup
                or (self.hold.active and not self.view().isVisible())):
            return
        self._marked = ([QPersistentModelIndex(self.model().index(row, 0))
                         for row in sorted(self.hold.rows())] if self.hold.active else [])
        self.update()
        self.bulk_selection_changed.emit()

    def choose_index(self, index: QModelIndex) -> None:
        if self.hold.active or len(self.hold.rows()) > 1:
            self.hold.active = True
            self.capture_selection()
            return
        if index.isValid():
            self.setCurrentIndex(index.row())
            self.activated.emit(index.row())
            self.hidePopup()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._list and isinstance(event, QKeyEvent):
            if event.type() == QEvent.Type.KeyPress:
                if event.key() == Qt.Key.Key_Escape and not self.hold.active:
                    self.hidePopup()
                    return True
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    if self.hold.active:
                        self.hidePopup()
                    else:
                        self.choose_index(self._list.currentIndex())
                    return True
                if (event.key() == Qt.Key.Key_A
                        and event.modifiers() == Qt.KeyboardModifier.ControlModifier):
                    self.hold.active = True
        return super().eventFilter(watched, event)

    def showPopup(self) -> None:
        if not self.isEnabled() or not self.count():
            return
        marked = [i for i in self._marked if i.isValid()]
        self._changing_popup = True
        try:
            self._list.ensurePolished()
            screen = self.screen().availableGeometry()
            row_height = max(20, self._list.sizeHintForRow(0))
            height = min(screen.height()//2,
                         row_height*min(self.count(), self.maxVisibleItems())+12)
            width = min(screen.width(), max(self.width(), self._list.sizeHintForColumn(0)+32))
            point = self.mapToGlobal(QPoint(0, self.height()))
            if point.y()+height > screen.bottom():
                point.setY(self.mapToGlobal(QPoint()).y()-height)
            point.setX(max(screen.left(), min(point.x(), screen.right()-width+1)))
            point.setY(max(screen.top(), point.y()))
            self._popup.setGeometry(point.x(), point.y(), width, height)
            self._popup.show()
            self._list.setCurrentIndex(self.model().index(self.currentIndex(), 0))
            self.hold.replace({i.row() for i in marked} if self.hold.active else
                              {self.currentIndex()})
            self._marked = marked
            self._list.scrollTo(self._list.currentIndex())
            self._list.setFocus(Qt.FocusReason.PopupFocusReason)
        finally:
            self._changing_popup = False

    def hidePopup(self) -> None:
        self._changing_popup = True
        try:
            self.hold.stop()
            self._popup.hide()
            super().hidePopup()
        finally:
            self._changing_popup = False
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        if not self.hold.active:
            super().paintEvent(event)
            return
        painter = QStylePainter(self)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        rows = self.selected_rows()
        text = (self.itemText(rows[0]) if len(rows) == 1 else
                tr('Выбрано: {0}').format(len(rows)))
        option.currentText = text  # type: ignore[attr-defined]
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, option)
