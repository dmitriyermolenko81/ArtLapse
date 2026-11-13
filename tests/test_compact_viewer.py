"""Compact controls share one row; versions live below the viewer, without legacy editing entry."""
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QPushButton
from test_cursor_playback import finish
from test_episode_panel import wait_until
from test_episode_roll import roll_movie as roll_movie

from sculptcut.ui import MainWindow


def test_compact_viewer_controls_and_lower_version_bar(roll_movie):
    app = QApplication.instance() or QApplication([])
    root, _, _, _, video = roll_movie
    window = MainWindow()
    dialog = None
    try:
        window.folder.setText(str(root))
        window.show()
        window.open_video(video, 'test')
        wait_until(app, lambda: window.selection_dialog is not None
                   and not window.selection_dialog.loading)
        dialog = window.selection_dialog
        app.processEvents()
        assert window.versions_panel.isVisible() and window.versions_panel.isEnabled()
        assert window.versions_panel.parentWidget() is window.result_actions_bar
        assert (window.versions_panel.mapTo(window, QPoint()).y()
                > window.main_splitter.geometry().bottom())
        assert not hasattr(window, 'episodes_button')
        assert not any(b.text() == 'Эпизоды монтажа' for b in window.findChildren(QPushButton))
        assert not any(b.accessibleName() in (
            'Применить настройки выбранной версии', 'Справка о готовых версиях')
            for b in window.findChildren(QPushButton))
        assert window.versions_panel.toolTip() == 'Выберите версию для просмотра'
        assert window.play_button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
        assert window.play_button.height() == 28
        assert not hasattr(window, 'episode_play_button')
        assert window.video_time.font().family() == window.play_button.font().family()
        assert window.variants.width() == 320
        assert window.variants.height() == 28
        assert window.versions_panel.width() < window.result_actions_bar.width() / 2
        window.resize(window.width() + 160, window.height())
        app.processEvents()
        assert window.variants.width() == 320
        assert window.play_button.height() == 28
        assert abs(window.video_time.geometry().center().y()
                   - window.play_button.geometry().center().y()) <= 1
        assert window.volume_control.height() == 28
        assert dialog.montage_seek.height() == 62
        assert dialog.undo.width() == 28 and not dialog.undo.icon().isNull()
        assert dialog.apply.height() == 34 and dialog.apply.text() == 'Применить правки'
        track = dialog.montage_seek
        assert track.navigation_tip.splitlines() == [
            'Клик — кадр · края — подрезка',
            'Пробел — просмотр · Q — разделить · Delete — убрать',
            'Ctrl+Z — отменить · Ctrl+Y — повторить',
            'Колесо — прокрутка · Ctrl+колесо — масштаб · Esc — отменить жест']
        original = dialog.session.plan
        point = QPointF(track.x_at(0), 8)
        app.sendEvent(track, QMouseEvent(QEvent.Type.MouseMove, point, point,
            Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
        assert track.toolTip().startswith('Начало ролика')
        assert dialog.session.plan == original
        window.play_button.click()
        wait_until(app, lambda: dialog.playback.is_advancing(), 10)
        window.play_button.click()
        assert not dialog.sequence.desired
        row, a, b = track.blocks[0]
        dialog.commit_edge(row, 'end', (a+b)//2)
        assert dialog.session.changed
        dialog.undo.click()
        assert not dialog.session.changed
    finally:
        if dialog is not None:
            finish(window, dialog, app)
        else:
            window._allow_close = True
            window.close()
