"""Fades are opt-in through the clip menu, without legacy menu clutter."""
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from test_cursor_playback import finish, open_editor
from test_episode_panel import wait_until
from test_music_edit import music_movie as music_movie

from sculptcut.music_edit import MusicClip, load_draft
from sculptcut.timeline_appearance import lane_bounds


def test_menu_add_remove_fades_history_and_default_paint(music_movie):
    root, video, clips = music_movie
    app = QApplication.instance() or QApplication([])
    window, source = open_editor(root, video, app)
    music = source.music_track
    assert music is not None
    try:
        music.commit([MusicClip(path=clips[0].path, start_us=0, end_us=3_000_000, at_us=0)])
        wait_until(app, lambda: music.lane.width() > 100)
        top, _ = lane_bounds(music.lane)
        def cyan_pixels():
            image = music.lane.grab().toImage()
            return sum(image.pixelColor(x, y).name() == '#8ce4ef'
                       for x in range(image.width()) for y in range(image.height()))
        assert cyan_pixels() == 0
        def popup():
            QTest.mouseClick(music.lane, Qt.MouseButton.RightButton,
                pos=QPoint(round(music.lane.x_at(1000)), top+15))
            assert music.options_menu.isVisible()
        popup()
        assert [a.text() for a in music.options_menu.actions()] == [
            'Добавить трек', 'Добавить плавное начало', 'Добавить затухание']
        action = music.fade_in_action
        QTest.mouseClick(music.options_menu, Qt.MouseButton.LeftButton,
            pos=music.options_menu.actionGeometry(action).center())
        assert music.clips[0].fade_in_us > 0 and music.clips[0].fade_out_us == 0
        assert cyan_pixels() > 0
        assert load_draft(root, video) == music.clips
        added = list(music.clips)
        music.undo()
        assert cyan_pixels() == 0 and music.clips[0].fade_in_us == 0
        music.redo()
        assert music.clips == added
        popup()
        assert music.fade_in_action.text() == 'Убрать плавное начало'
        QTest.mouseClick(music.options_menu, Qt.MouseButton.LeftButton,
            pos=music.options_menu.actionGeometry(music.fade_in_action).center())
        assert cyan_pixels() == 0
        popup()
        QTest.mouseClick(music.options_menu, Qt.MouseButton.LeftButton,
            pos=music.options_menu.actionGeometry(music.fade_out_action).center())
        assert music.clips[0].fade_out_us > 0 and music.clips[0].fade_in_us == 0
        assert load_draft(root, video) == music.clips
        assert window.grab().save(str(root/'optional.png'))
    finally:
        finish(window, source, app)
