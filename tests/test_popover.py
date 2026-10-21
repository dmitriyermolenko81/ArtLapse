from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QSettings, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from sculptcut.feedback_dialog import FeedbackDialog
from sculptcut.popover import exec_anchored
from sculptcut.presentation_dialog import PresentationDialog
from sculptcut.ui_theme import STYLE


@pytest.fixture(scope="session")
def panel_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_popup_source_menu_can_apply_without_dismissing_panel(
    tmp_path: Path, panel_app: QApplication,
) -> None:
    app = panel_app
    previous_style = app.style().objectName()
    # Fusion exposes the Qt menu; Windows' OS-native menu has no visible QListView.
    app.setStyle("Fusion")
    owner = QWidget()
    owner.setStyleSheet(STYLE)
    owner.resize(1120, 700)
    button = QPushButton("Presentation", owner)
    button.move(240, 70)
    owner.show()
    source = tmp_path / "source.mp4"
    source.touch()
    dialog = PresentationDialog(None, 5, "start", [source], owner)
    failures: list[BaseException] = []

    def configure() -> None:
        try:
            assert not dialog.isWindow() and dialog.window() is owner
            header = dialog.findChild(QWidget, "dialogHeader")
            assert header is not None and header.isHidden()
            dialog.video.showPopup()
            app.processEvents()
            view = dialog.video.view()
            assert view.isVisible()
            index = view.model().index(1, 0)
            QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                             pos=view.visualRect(index).center())
            app.processEvents()
            assert dialog.isVisible() and dialog.path == source
            for _ in range(12):
                QTest.keyClick(QApplication.focusWidget() or dialog, Qt.Key.Key_Tab)
                focus = QApplication.focusWidget()
                assert focus is dialog or (focus is not None and dialog.isAncestorOf(focus))
            dialog.seconds.setValue(6)
            dialog.position.group.button(1).click()
            dialog.apply_button.click()
        except BaseException as error:
            failures.append(error)
            dialog.reject()

    QTimer.singleShot(50, configure)
    try:
        result = exec_anchored(dialog, button)
        assert not failures, failures
        assert result == dialog.DialogCode.Accepted
        assert dialog.path == source and dialog.placement == "end" and dialog.seconds.value() == 6
    finally:
        dialog.close()
        owner.close()
        owner.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        app.setStyle(previous_style)


def test_popup_tracks_owner_and_outside_click_saves_feedback_draft(
    tmp_path: Path, panel_app: QApplication,
) -> None:
    app = panel_app
    owner = QWidget()
    owner.setStyleSheet(STYLE)
    owner.resize(1280, 800)
    anchor = QPushButton("Feedback", owner)
    anchor.move(1150, 20)
    behind = QPushButton("Do not activate", owner)
    behind.setGeometry(10, 10, 150, 40)
    clicks: list[bool] = []
    behind.clicked.connect(clicks.append)
    settings = QSettings(str(tmp_path / "draft.ini"), QSettings.Format.IniFormat)
    dialog = FeedbackDialog(settings, "timelapse", owner,
                            endpoint="https://example.invalid/feedback")
    owner.show()
    failures: list[BaseException] = []

    def inspect() -> None:
        try:
            dialog.message.setPlainText("Сохрани этот отзыв при закрытии панели снаружи")
            dialog.more_button.click()
            owner.resize(1120, 700)
            owner.move(owner.x() + 20, owner.y() + 20)
            anchor.move(1000, 640)
            app.processEvents()
            app.processEvents()
            bounds = owner.rect()
            assert bounds.contains(dialog.geometry())
            assert dialog.geometry().bottom() < anchor.mapTo(owner, QPoint()).y()
            QTest.mouseClick(owner.windowHandle(), Qt.MouseButton.LeftButton,
                             pos=QPoint(30, 25))
            app.processEvents()
            assert not dialog.isVisible()
        except BaseException as error:
            failures.append(error)
            dialog.reject()

    QTimer.singleShot(50, inspect)
    try:
        assert exec_anchored(dialog, anchor) == dialog.DialogCode.Rejected
        assert not failures, failures
        assert not clicks
        assert "Сохрани этот отзыв" in str(settings.value("feedback_draft"))
    finally:
        dialog.close()
        owner.close()
        owner.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_popup_escape_preserves_feedback_draft(
    tmp_path: Path, panel_app: QApplication,
) -> None:
    app = panel_app
    owner = QWidget()
    owner.resize(1120, 700)
    anchor = QPushButton("Feedback", owner)
    owner.show()
    settings = QSettings(str(tmp_path / "escape.ini"), QSettings.Format.IniFormat)
    dialog = FeedbackDialog(settings, "timelapse", owner,
                            endpoint="https://example.invalid/feedback")
    dialog.message.setPlainText("Сохрани этот отзыв после нажатия Escape")
    QTimer.singleShot(50, lambda: QTest.keyClick(dialog.message, Qt.Key.Key_Escape))
    try:
        assert exec_anchored(dialog, anchor) == dialog.DialogCode.Rejected
        assert "Сохрани этот отзыв" in str(settings.value("feedback_draft"))
    finally:
        dialog.close()
        owner.close()
        owner.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
