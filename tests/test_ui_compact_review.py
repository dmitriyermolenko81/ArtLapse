"""The compact editor keeps important controls visible and keyboard accessible."""
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from sculptcut.ui import MainWindow


def test_compact_settings_show_current_mode_and_cover_with_statistics():
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.resize(1120, 700)
        window.show()
        window.show_statistics('Монтаж · Исходники: 05:07:12 · Вырезано: 04:56:02 (96.4%) · '
                               'Оставлено: 11:10 · Ролик: 05:00 · Эпизодов: 213')
        for preset, title in (('timelapse', 'Таймлапс · эпизоды'),
                              ('music_shorts', '3D · видео под музыку'),
                              ('photoshop_music_shorts', 'Photoshop · видео под музыку')):
            window.preset.setCurrentIndex(window.preset.findData(preset))
            QTest.qWait(100)
            assert window.mode_settings_title.text() == title
            viewport = window.settings_scroll.viewport()
            bottom = window.intro_image.mapTo(viewport, QPoint()).y() + window.intro_image.height()
            assert window.settings_scroll.verticalScrollBar().maximum() == 0
            assert bottom <= viewport.height()
    finally:
        window._allow_close = True
        window.close()
        app.processEvents()


def test_duration_can_be_adjusted_and_edited_with_keyboard():
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.show()
        field = window.duration_card.seconds
        field.setFocus()
        before = window.target.value()
        QTest.keyClick(field, Qt.Key.Key_F2)
        QTest.keyClick(field, Qt.Key.Key_Up)
        assert round(window.target.value() * 60) == round(before * 60) + 1
        QTest.keyClick(field, Qt.Key.Key_F2)
        QTest.keyClicks(field, '25')
        QTest.keyClick(field, Qt.Key.Key_Return)
        assert round(window.target.value() * 60) % 60 == 25
    finally:
        window._allow_close = True
        window.close()
        app.processEvents()
