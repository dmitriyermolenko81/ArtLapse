"""Timeline navigation changes the viewport/playhead, never source decisions."""
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from test_episode_panel import wait_until
from test_episode_roll import roll_movie as roll_movie

from sculptcut.simple_timeline import SimpleTimeline


def wheel(app, track, mods=Qt.KeyboardModifier.NoModifier, pixels=None):
    pos = QPointF(210, 40)
    event = QWheelEvent(pos, pos, pixels or QPoint(), QPoint(0, 120),
                        Qt.MouseButton.NoButton, mods, Qt.ScrollPhase.ScrollUpdate, False)
    app.sendEvent(track, event)


def test_middle_pan_wheel_zoom_and_keys_never_commit_edits():
    app = QApplication.instance() or QApplication([])
    track = SimpleTimeline()
    track.resize(620, 86)
    track.setRange(0, 60000)
    track.blocks = [(0, 0, 10000), (1, 10000, 30000), (2, 30000, 60000)]
    changes, seeks = [], []
    track.edge_commit.connect(lambda *args: changes.append(args))
    track.movie_commit.connect(lambda *args: changes.append(args))
    track.sliderMoved.connect(seeks.append)
    try:
        track.show()
        track.set_view(20000, 10000)
        QTest.mousePress(track, Qt.MouseButton.MiddleButton, pos=QPoint(310, 40))
        QTest.mouseMove(track, QPoint(190, 40))
        QTest.mouseRelease(track, Qt.MouseButton.MiddleButton, pos=QPoint(190, 40))
        assert track.view_start == 22000
        assert not seeks and not changes
        wheel(app, track)
        assert track.view_start == 20500 and track.view_span == 10000
        anchor = track.time_at(210)
        wheel(app, track, Qt.KeyboardModifier.ControlModifier)
        assert track.view_span == 7000
        assert abs(track.time_at(210) - anchor) <= 1
        wheel(app, track, pixels=QPoint(-60, 0))
        assert track.view_start > 20500
        QTest.keyClick(track, Qt.Key.Key_F)
        assert track.view_start == 0 and track.view_span == 60000
        QTest.keyClick(track, Qt.Key.Key_Home)
        QTest.keyClick(track, Qt.Key.Key_Right)
        assert track.value() == 33
        QTest.keyClick(track, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
        assert track.value() == 367
        QTest.keyClick(track, Qt.Key.Key_Down)
        assert track.value() == 10000
        QTest.keyClick(track, Qt.Key.Key_End)
        assert track.value() == 60000
        track.set_view(20000, 10000)
        QTest.mousePress(track, Qt.MouseButton.MiddleButton, pos=QPoint(300, 40))
        QTest.mouseMove(track, QPoint(200, 40))
        QTest.keyClick(track, Qt.Key.Key_Escape)
        QTest.mouseRelease(track, Qt.MouseButton.MiddleButton, pos=QPoint(200, 40))
        assert track.view_start == 20000
        assert not changes
    finally:
        track.close()


def test_navigation_in_main_editor_and_local_undo_shortcuts(roll_movie):
    from sculptcut.ui import MainWindow

    app = QApplication.instance() or QApplication([])
    root, _, _, _, video = roll_movie
    window = MainWindow()
    try:
        window.folder.setText(str(root))
        window.show()
        window.open_video(video, "test")
        window.player.pause()
        window.cut_button.click()
        dialog = window.selection_dialog
        wait_until(app, lambda: not dialog.loading)
        track = dialog.montage_seek
        original = dialog.session.plan
        track.set_view(1000, 2000)
        QTest.mousePress(track, Qt.MouseButton.MiddleButton, pos=QPoint(310, 40))
        QTest.mouseMove(track, QPoint(210, 40))
        QTest.mouseRelease(track, Qt.MouseButton.MiddleButton, pos=QPoint(210, 40))
        assert dialog.session.plan == original and not dialog.session.undo_stack
        QTest.keyClick(track, Qt.Key.Key_F)
        QTest.keyClick(track, Qt.Key.Key_Down)
        wait_until(app, lambda: dialog.montage.currentRow() == 1)
        QTest.keyClick(track, Qt.Key.Key_Up)
        wait_until(app, lambda: dialog.montage.currentRow() == 0)
        QTest.keyClick(track, Qt.Key.Key_Space)
        assert dialog.sequence.desired
        QTest.keyClick(track, Qt.Key.Key_Space)
        assert not dialog.sequence.desired
        row, a, b = track.blocks[0]
        dialog.commit_edge(row, "end", (a+b)//2)
        changed = dialog.session.plan
        assert changed != original
        QTest.keyClick(track, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert dialog.session.plan == original
        QTest.keyClick(track, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
        assert dialog.session.plan == changed
    finally:
        window._allow_close = True
        window.close()
        app.processEvents()
