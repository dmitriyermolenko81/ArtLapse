"""Real Qt long-press selection without modifiers or accidental activation."""
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from test_episode_panel import wait_until
from test_project_cleanup import project_root as project_root

from sculptcut.hold_selection import SelectionCombo
from sculptcut.source_cards import SourceCards


def hold_range(view, first, last):
    app = QApplication.instance()
    view.scrollTo(view.model().index(first, 0))
    app.processEvents()
    a = view.visualRect(view.model().index(first, 0)).center()
    b = view.visualRect(view.model().index(last, 0)).center()
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=a)
    QTest.qWait(QApplication.startDragTime()+60)
    QTest.mouseMove(view.viewport(), b)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=b)
    app.processEvents()


def test_source_cards_hold_range_toggle_and_escape():
    app = QApplication.instance() or QApplication([])
    cards = SourceCards()
    cards.addItems([f'source{i}.mp4' for i in range(5)])
    cards.resize(400, 480)
    cards.show()
    QTest.qWait(50)
    try:
        hold_range(cards, 0, 2)
        assert [cards.row(i) for i in cards.selectedItems()] == [0, 1, 2]
        point = cards.visualItemRect(cards.item(1)).center()
        QTest.mouseClick(cards.viewport(), Qt.MouseButton.LeftButton, pos=point)
        assert [cards.row(i) for i in cards.selectedItems()] == [0, 2]
        QTest.keyClick(cards, Qt.Key.Key_Escape)
        assert not cards.hold_selection.active
        assert not cards.selectedItems()
    finally:
        cards.close()
        app.processEvents()


def test_combo_hold_keeps_popup_and_selection_without_opening_items():
    app = QApplication.instance() or QApplication([])
    combo = SelectionCombo()
    combo.addItems([f'version{i}.mp4' for i in range(8)])
    combo.resize(350, 35)
    activated = []
    combo.activated.connect(activated.append)
    combo.show()
    combo.showPopup()
    wait_until(app, combo.view().isVisible)
    try:
        hold_range(combo.view(), 1, 4)
        assert combo.selected_rows() == [1, 2, 3, 4]
        assert not activated and combo.view().isVisible()
        point = combo.view().visualRect(combo.model().index(6, 0)).center()
        QTest.mouseMove(combo.view().viewport(), point)
        app.processEvents()
        assert combo.selected_rows() == [1, 2, 3, 4]
        combo.hidePopup()
        assert combo.selected_rows() == [1, 2, 3, 4]
        combo.showPopup()
        wait_until(app, combo.view().isVisible)
        assert combo.selected_rows() == [1, 2, 3, 4]
        for row in (1, 2, 3, 4):
            point = combo.view().visualRect(combo.model().index(row, 0)).center()
            QTest.mouseClick(combo.view().viewport(), Qt.MouseButton.LeftButton, pos=point)
        assert combo.selected_rows() == [] and not activated
        QTest.keyClick(combo.view(), Qt.Key.Key_Escape)
        assert not combo.hold.active
        combo.showPopup()
        wait_until(app, combo.view().isVisible)
        point = combo.view().visualRect(combo.model().index(2, 0)).center()
        QTest.mouseClick(combo.view().viewport(), Qt.MouseButton.LeftButton, pos=point)
        assert activated == [2]
        assert combo.selected_rows() == [2]
    finally:
        combo.hidePopup()
        combo.close()
        app.processEvents()


def test_hold_auto_scroll_and_quick_click_does_not_enable_bulk():
    app = QApplication.instance() or QApplication([])
    combo = SelectionCombo()
    combo.addItems([f'version{i}.mp4' for i in range(40)])
    combo.setMaxVisibleItems(5)
    combo.show()
    combo.showPopup()
    wait_until(app, combo.view().isVisible)
    view = combo.view()
    try:
        start = view.visualRect(combo.model().index(0, 0)).center()
        edge = view.viewport().rect().bottomLeft()
        edge.setX(start.x())
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.qWait(QApplication.startDragTime()+60)
        QTest.mouseMove(view.viewport(), edge)
        wait_until(app, lambda: view.verticalScrollBar().value() > 0)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=edge)
        assert len(combo.selected_rows()) > 5
        assert not combo.hold.scroll.isActive()
        QTest.keyClick(view, Qt.Key.Key_Escape)
        combo.showPopup()
        app.processEvents()
        point = view.visualRect(combo.model().index(0, 0)).center()
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
        QTest.qWait(QApplication.startDragTime()+60)
        assert not combo.hold.active and not combo.hold.timer.isActive()
        assert combo.selected_rows() == [0]
    finally:
        combo.hidePopup()
        combo.close()
        app.processEvents()



def test_source_batch_remove_only_removes_list_entries(project_root):
    from sculptcut.ui import MainWindow

    app = QApplication.instance() or QApplication([])
    root = project_root
    paths = [root.parent/'исходник.mp4', root/'renders/готовый.mp4',
             root/'previews/готовый.mp4']
    before = {path: path.read_bytes() for path in paths}
    window = MainWindow()
    try:
        window.show()
        window.set_sources(paths)
        app.processEvents()
        hold_range(window.sources, 0, 1)
        assert len(window.sources.selectedItems()) == 2
        window.remove_button.click()
        assert window.listed_source_paths() == [paths[2]]
        assert all(path.read_bytes() == value for path, value in before.items())
    finally:
        window.close()
        app.processEvents()


def test_cleanup_hold_limits_deletion_to_selected_video_group(project_root):
    from sculptcut.cleanup_dialog import CleanupDialog
    from sculptcut.project_cleanup import cleanup_files
    from sculptcut.ui import MainWindow

    app = QApplication.instance() or QApplication([])
    root = project_root
    owner = MainWindow()
    dialog = CleanupDialog(root, cleanup_files(root), owner)
    try:
        dialog.categories['renders'].setChecked(True)
        dialog.categories['previews'].setChecked(True)
        dialog.details_button.setChecked(True)
        dialog.show()
        app.processEvents()
        all_files = dialog.selected_files()
        assert len({f.video for f in all_files}) >= 2
        hold_range(dialog.file_list, 0, 0)
        selected = dialog.selected_files()
        assert selected and len(selected) < len(all_files)
        assert len({f.video for f in selected}) == 1
        assert len(dialog.file_list.selectedItems()) == len(selected)
        assert any(f.path == f.video for f in selected)
        QTest.keyClick(dialog.file_list, Qt.Key.Key_Escape)
        assert dialog.selected_files() == all_files
        QTest.mouseClick(dialog.file_list.viewport(), Qt.MouseButton.LeftButton,
                        pos=dialog.file_list.visualItemRect(dialog.file_list.item(0)).center())
        assert dialog.selected_files() == selected
        assert len(dialog.file_list.selectedItems()) == len(selected)
    finally:
        dialog.close()
        owner.close()
        app.processEvents()
