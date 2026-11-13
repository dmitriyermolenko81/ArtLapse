"""Late decoder frames cannot replace a selected scene; alternatives are visible."""

from pathlib import Path

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QWidget
from test_episode_roll import roll_movie as roll_movie
from test_episode_roll import wait_loaded

from sculptcut.episode_playback import EpisodePlayback
from sculptcut.episode_roll_dialog import EpisodeRollDialog


def test_decoder_discards_start_frame_after_seeking_to_later_scene() -> None:
    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    playback = EpisodePlayback(owner)
    playback.limit_frames = True
    playback.start, playback.end = 5000, 8000
    received = []
    playback.image_ready.connect(lambda image,stamp:received.append(stamp))

    class Frame:
        def __init__(self, stamp):
            self.stamp = stamp

        def isValid(self):
            return True

        def startTime(self):
            return self.stamp

        def toImage(self):
            assert self.stamp == 6_000_000  # Wrong/stale frames must not even be converted.
            return QImage(32,32,QImage.Format.Format_RGB32)

    try:
        playback.frame_ready(Frame(0))
        playback.frame_ready(Frame(4_900_000))
        playback.frame_ready(Frame(8_000_000))
        assert received == []
        playback.frame_ready(Frame(6_000_000))
        assert received == [6000]
    finally:
        playback.shutdown()
        owner.close()
        app.processEvents()


def test_visible_options_bounds_click_and_paused_stale_frame(roll_movie, tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    root,project,plan,episodes,video = roll_movie
    owner = QWidget()
    dialog = EpisodeRollDialog(root,video,owner)
    try:
        dialog.show()
        wait_loaded(dialog)
        dialog.select_slot(1)
        assert dialog.options_strip.count() == 5
        assert "сцена 1" in dialog.bounds.text() and "сцена 3" in dialog.bounds.text()
        assert [d.episode_id for d in dialog.visible_options] == [e.id for e in episodes[1:6]]
        assert "В видео" in dialog.options_strip.item(3).text()
        assert dialog.options_strip.item(3).font().bold()
        dialog.choose_option(dialog.options_strip.item(4))
        assert dialog.roll.choices[1].episode_id == episodes[5].id
        assert dialog.options_strip.currentRow() == 4
        assert "В видео" in dialog.options_strip.item(3).text()
        assert "Выбрана замена" in dialog.options_strip.item(4).text()
        assert "00:12.0–00:15.0" in dialog.bounds.text()
        dialog.replay(False)
        assert dialog.options_strip.currentRow() == 3
        assert "В видео" in dialog.options_strip.item(3).text()
        dialog.replay(True)
        assert "Выбрана замена" in dialog.options_strip.item(4).text()
        loop = QEventLoop()
        QTimer.singleShot(1800,loop.quit)
        loop.exec()
        assert not dialog.frame_image.isNull()
        assert dialog.frame_image.pixelColor(10,10).blue() > 80
        identity = dialog.frame_image.cacheKey()
        stale = QImage(320,180,QImage.Format.Format_RGB32)
        stale.fill(QColor("red"))
        dialog.video_frame(stale,0)
        assert dialog.frame_image.cacheKey() == identity
        assert any(not dialog.options_strip.item(i).icon().isNull() for i in range(5))
        dialog.grab().save(str(tmp_path/"choices.png"))
        assert not list((root/"previews").glob("*.mp4"))
    finally:
        dialog.reject()
        dialog.deleteLater()
        owner.close()
        app.processEvents()
