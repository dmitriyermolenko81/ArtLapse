import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from sculptcut.ui import MainWindow


@pytest.mark.parametrize("unit", ["hours", "minutes", "seconds"])
@pytest.mark.parametrize("delta", [-120, 120])
def test_wheel_does_not_select_duration_text(unit: str, delta: int) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.show()
        app.processEvents()
        window.target.setValue(75)
        field = getattr(window.duration_card, unit)
        field.setFocus()
        field.selectAll()
        editor = field.lineEdit()
        assert editor is not None and editor.hasSelectedText()
        before = window.target.value()
        point = QPointF(field.rect().center())
        wheel = QWheelEvent(point, point, QPoint(), QPoint(0, delta),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(field, wheel)
        app.processEvents()
        assert window.target.value() != before
        assert not editor.hasSelectedText()
        assert editor.isReadOnly()
        after = window.target.value()
        QTest.keyClicks(field, "123")
        QTest.keyClick(field, Qt.Key.Key_Up)
        assert window.target.value() == after
        QTest.mouseClick(editor, Qt.MouseButton.RightButton)
        assert editor.isReadOnly()
        QTest.mouseClick(editor, Qt.MouseButton.LeftButton)
        assert not editor.isReadOnly()
        # A deliberate left click enables normal editing.
        field.selectAll()
        assert editor.hasSelectedText()
    finally:
        window.close()
        app.processEvents()


def test_manual_duration_input_requires_click_and_locks_after_wheel() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.show()
        app.processEvents()
        field = window.duration_card.minutes
        editor = field.lineEdit()
        assert editor is not None and editor.isReadOnly()
        field.setFocus()
        assert editor.isReadOnly()
        QTest.mouseClick(editor, Qt.MouseButton.LeftButton)
        assert not editor.isReadOnly()
        field.selectAll()
        QTest.keyClicks(field, "12")
        QTest.keyClick(field, Qt.Key.Key_Return)
        assert window.target.value() == 12
        assert editor.isReadOnly()
        QTest.mouseClick(editor, Qt.MouseButton.LeftButton)
        window.output_name.setFocus()
        app.processEvents()
        assert editor.isReadOnly()
    finally:
        window.close()
        app.processEvents()


def test_duration_neighbors_click_wheel_and_limits() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.show()
        app.processEvents()
        card = window.duration_card
        window.target.setValue((14 * 60 + 50) / 60)
        assert (card.minutes.neighbor_text(-1), card.minutes.neighbor_text(1)) == ("13", "15")
        assert (card.seconds.neighbor_text(-1), card.seconds.neighbor_text(1)) == ("49", "51")
        QTest.mouseClick(card.seconds, Qt.MouseButton.LeftButton,
                         pos=QPoint(card.seconds.width() // 2, card.seconds.height() - 2))
        assert card.seconds.value() == 51
        window.target.setValue(1)
        QTest.mouseClick(card.seconds, Qt.MouseButton.LeftButton,
                         pos=QPoint(card.seconds.width() // 2, 2))
        assert (card.minutes.value(), card.seconds.value()) == (0, 59)
        point = QPointF(card.seconds.rect().center())
        wheel = QWheelEvent(point, point, QPoint(), QPoint(0, 120),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(card.seconds, wheel)
        assert (card.minutes.value(), card.seconds.value()) == (1, 0)
        editor = card.seconds.lineEdit()
        assert editor is not None and not editor.hasSelectedText()
        window.shorts_mode.click()
        window.target.setValue(300)
        assert card.seconds.neighbor_text(1) == ""
        assert card.minutes.neighbor_text(1) == ""
        window.target.setValue(0)
        assert card.seconds.neighbor_text(-1) == ""
        assert card.minutes.neighbor_text(-1) == ""
        assert not card.hours_container.isVisible()
    finally:
        window.close()
        app.processEvents()
