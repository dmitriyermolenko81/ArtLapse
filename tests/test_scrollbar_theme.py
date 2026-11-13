"""Shared scrollbar painting and interaction in panels, dialogs and combo popups."""
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QListWidget,
    QScrollArea,
    QStyle,
    QStyleOptionSlider,
    QVBoxLayout,
    QWidget,
)

from sculptcut.ui_theme import STYLE, apply_dialog_theme


def assert_thumb_and_drag(bar):
    app = QApplication.instance()
    app.processEvents()
    assert bar.maximum() > 0
    vertical = bar.orientation() == Qt.Orientation.Vertical
    assert (bar.width() if vertical else bar.height()) == 12
    image = bar.grab().toImage()
    assert any(image.pixelColor(x, y).name() == '#526b86'
               for x in range(image.width()) for y in range(image.height()))
    option = QStyleOptionSlider()
    bar.initStyleOption(option)
    style = bar.style()
    for control in (QStyle.SubControl.SC_ScrollBarAddLine,
                    QStyle.SubControl.SC_ScrollBarSubLine):
        assert style.subControlRect(QStyle.ComplexControl.CC_ScrollBar, option,
                                    control, bar).isEmpty()
    handle = style.subControlRect(QStyle.ComplexControl.CC_ScrollBar, option,
                                 QStyle.SubControl.SC_ScrollBarSlider, bar)
    QTest.mousePress(bar, Qt.MouseButton.LeftButton, pos=handle.center())
    target = QPoint(bar.width()//2, bar.height()-10) if vertical else (
        QPoint(bar.width()-10, bar.height()//2))
    QTest.mouseMove(bar, target)
    QTest.mouseRelease(bar, Qt.MouseButton.LeftButton, pos=target)
    assert bar.value() > 0


def test_shared_scrollbars_and_popup_use_modern_theme(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = QWidget()
    window.setStyleSheet(STYLE)
    layout = QVBoxLayout(window)
    scroll = QScrollArea()
    content = QWidget()
    content.setFixedSize(900, 1000)
    scroll.setWidget(content)
    layout.addWidget(scroll)
    combo = QComboBox()
    combo.addItems([f'Version {i:03d}' for i in range(100)])
    combo.setMaxVisibleItems(7)
    layout.addWidget(combo)
    window.resize(500, 400)
    window.show()
    QTest.qWait(50)
    try:
        assert_thumb_and_drag(scroll.verticalScrollBar())
        assert_thumb_and_drag(scroll.horizontalScrollBar())
        bar = scroll.verticalScrollBar()
        bar.setValue(0)
        point = QPointF(scroll.viewport().rect().center())
        app.sendEvent(scroll.viewport(), QWheelEvent(point, point, QPoint(), QPoint(0, -120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False))
        assert bar.value() > 0
        combo.setCurrentIndex(0)
        combo.setFocus()
        QTest.keyClick(combo, Qt.Key.Key_Down)
        assert combo.currentIndex() == 1
        # Popup rendering is checked without relying on Windows foreground focus.
        # This worker's popup is dismissed by the desktop before activation settles.
        combo.showPopup()
        app.processEvents()
        bar = combo.view().verticalScrollBar()
        assert_thumb_and_drag(bar)
        assert combo.view().window().grab().save(str(tmp_path/'popup.png'))
        combo.hidePopup()
        assert window.grab().save(str(tmp_path/'panels.png'))
    finally:
        combo.hidePopup()
        window.close()
        app.processEvents()


def test_separate_dialog_scrollbar_inherits_shared_style(tmp_path):
    app = QApplication.instance() or QApplication([])
    dialog = QDialog()
    apply_dialog_theme(dialog)
    layout = QVBoxLayout(dialog)
    items = QListWidget()
    items.addItems([f'Item {i}' for i in range(100)])
    layout.addWidget(items)
    dialog.resize(380, 300)
    dialog.show()
    QTest.qWait(30)
    try:
        assert_thumb_and_drag(items.verticalScrollBar())
        assert dialog.grab().save(str(tmp_path/'dialog.png'))
    finally:
        dialog.close()
        app.processEvents()
