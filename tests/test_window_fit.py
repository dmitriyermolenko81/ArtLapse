from pathlib import Path

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QVideoFrame
from PySide6.QtWidgets import QApplication, QWidget

from sculptcut.ui import MainWindow


def settle(app: QApplication) -> None:
    for _ in range(6):
        app.processEvents()


@pytest.mark.parametrize("size", [(1440, 860), (1280, 720), (1120, 700)])
def test_all_presets_fit_with_intro_and_saved_versions(
    tmp_path: Path, size: tuple[int, int],
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    image = QImage(640, 360, QImage.Format.Format_RGB32)
    image.fill(0xFF778899)
    path = tmp_path / "intro.png"
    assert image.save(str(path))
    try:
        window.resize(*size)
        window.show()
        window.intro_image.setText(str(path))
        window.versions_panel.show()
        window.variants.addItem("Сохранённая версия")
        window.cut_section.show()
        window.cut_button.show()
        settle(app)
        for header, panel in ((window.brand_header, window.sidebar_scroll),
                              (window.project_header, window.viewer_scroll),
                              (window.project_actions, window.settings_scroll)):
            assert header.mapTo(window, QPoint()).x() == panel.mapTo(window, QPoint()).x()
            assert header.width() == panel.width()
        assert window.recent_button.parentWidget() is window.project_header
        assert window.recent_button.size().width() == window.recent_button.size().height() == 36
        assert window.recent_button.x() > window.open_project_button.geometry().right()
        assert window.project_header.rect().contains(window.recent_button.geometry())
        assert window.recent_button.geometry().right() == window.project_header.rect().right()
        assert window.folder.x() == 0
        assert window.folder.geometry().right() < window.new_project_button.x()
        for preset in ("timelapse", "speed", "shorts", "music_shorts", "beat_shorts",
                       "photoshop_shorts", "photoshop_beat_shorts"):
            window.preset.setCurrentIndex(window.preset.findData(preset))
            settle(app)
            for scroll in (window.sidebar_scroll, window.viewer_scroll, window.settings_scroll):
                assert scroll.verticalScrollBar().maximum() == 0, (size, preset)
            viewport = window.settings_scroll.viewport()
            for control in (window.duration_card, window.music, window.intro_image,
                            window.intro_preview, window.music_selection):
                top_left = control.mapTo(viewport, QPoint())
                assert viewport.rect().contains(top_left)
                assert viewport.rect().contains(top_left + control.rect().bottomRight())
    finally:
        window.close()
        settle(app)


def test_paused_video_refits_when_window_shrinks() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.show()
        settle(app)
        image = QImage(1080, 1920, QImage.Format.Format_RGB32)
        image.fill(0xFF778899)
        window.show_video_frame(QVideoFrame(image))
        window.resize(1120, 700)
        settle(app)
        pixmap = window.video.pixmap()
        assert pixmap.height() <= window.video.height()
        assert pixmap.width() <= window.video.width()
        assert abs(pixmap.width() / pixmap.height() - 1080 / 1920) < 0.01
    finally:
        window.close()
        settle(app)


@pytest.mark.parametrize("size", [(1440, 860), (1280, 720), (1120, 700)])
def test_settings_keep_positions_across_modes(size: tuple[int, int]) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.resize(*size)
        window.show()
        settle(app)

        def geometry(control: QWidget) -> tuple[int, int, int, int]:
            point = control.mapTo(window.settings_panel, QPoint())
            return point.x(), point.y(), control.width(), control.height()

        controls = (window.duration_card, window.duration_card.minutes,
                    window.duration_card.seconds, window.area_stack, window.music,
                    window.music_selection, window.intro_image, *window.settings_dividers)
        baseline = [geometry(control) for control in controls]
        assert window.audio_label.text() == "Звук"
        assert len(window.settings_dividers) == 4
        assert all(line.height() == 1 for line in window.settings_dividers)
        snapshot = window.settings_panel.grab().toImage()
        ratio = snapshot.devicePixelRatio()
        for line in window.settings_dividers:
            point = line.mapTo(window.settings_panel, QPoint(line.width() // 2, 0))
            line_color = snapshot.pixelColor(round(point.x() * ratio), round(point.y() * ratio))
            panel_color = snapshot.pixelColor(round(2 * ratio), round(point.y() * ratio))
            assert line_color.lightness() > panel_color.lightness()

        assert all(line.width() == window.music.parentWidget().width()
                   for line in window.settings_dividers)
        assert geometry(window.audio_divider)[1] < geometry(window.music)[1]
        assert geometry(window.music)[1] < geometry(window.music_selection)[1]
        assert geometry(window.music_selection)[1] < geometry(window.intro_divider)[1]

        preset_slots = [geometry(window.preset.buttons[value])
                        for value in ("timelapse", "speed")]
        area_slots = [geometry(window.frame_mode.buttons[value])
                      for value in ("full", "viewport")]
        for preset in ("shorts", "music_shorts", "beat_shorts", "photoshop_shorts",
                       "photoshop_beat_shorts", "speed", "timelapse"):
            window.preset.setCurrentIndex(window.preset.findData(preset))
            settle(app)
            assert [geometry(control) for control in controls] == baseline, (size, preset)
            visible = [button for button in window.preset.buttons.values() if button.isVisible()]
            assert [geometry(button) for button in visible[:2]] == preset_slots, (size, preset)
            if preset not in ("timelapse", "speed"):
                assert [geometry(button) for button in
                        window.shorts_layout.buttons.values()] == area_slots
        window.frame_mode.setCurrentIndex(window.frame_mode.findData("viewport"))
        settle(app)
        assert not hasattr(window, "crop_button")
        assert [geometry(button) for button in window.frame_mode.buttons.values()] == area_slots
        assert [geometry(control) for control in controls] == baseline
    finally:
        window.close()
        settle(app)
