"""A play request survives asynchronous loading/seek and stale EOF notifications."""

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QApplication, QWidget
from test_episode_panel import wait_until

from sculptcut.episode_playback import EpisodePlayback
from sculptcut.media import discover, run_process


class DelayedPlayer:
    def __init__(self):
        self.pos = 9000
        self.starts = 0
        self.seeks = []
        self.status = QMediaPlayer.MediaStatus.LoadedMedia
        self.state = QMediaPlayer.PlaybackState.PausedState

    def source(self):
        return QUrl.fromLocalFile("C:/selected.mp4")

    def position(self):
        return self.pos

    def mediaStatus(self):
        return self.status

    def playbackState(self):
        return self.state

    def setPosition(self, position):
        self.seeks.append(position)  # Decoder intentionally still reports old EOF.

    def play(self):
        self.starts += 1
        self.state = QMediaPlayer.PlaybackState.PlayingState

    def pause(self):
        self.state = QMediaPlayer.PlaybackState.PausedState


def test_old_end_during_seek_does_not_cancel_play_request() -> None:
    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    playback = EpisodePlayback(owner)
    native = playback.player
    fake = DelayedPlayer()
    playback.player = fake
    try:
        playback.start, playback.end = 5000,7000
        playback.play()
        playback.check_end()
        playback.loaded(QMediaPlayer.MediaStatus.EndOfMedia)
        assert playback.want_play and playback.awaiting_seek
        playback.resume()
        assert fake.starts == 1 and fake.seeks[-1] == 5000
        playback.pause()
        playback.resume()
        assert fake.starts == 1  # A queued resume cannot undo the user's pause.
    finally:
        playback.player = native
        playback.shutdown()
        owner.close()
        app.processEvents()


def test_pause_cancels_play_queued_while_source_loads() -> None:
    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    playback = EpisodePlayback(owner)
    native = playback.player
    fake = DelayedPlayer()
    fake.status = QMediaPlayer.MediaStatus.LoadingMedia
    playback.player = fake
    try:
        playback.start,playback.end,playback.pending = 5000,7000,5000
        playback.play()
        assert playback.want_play
        playback.pause()
        fake.status = QMediaPlayer.MediaStatus.LoadedMedia
        playback.loaded(fake.status)
        playback.resume()
        assert not playback.want_play and fake.starts == 0
    finally:
        playback.player = native
        playback.shutdown()
        owner.close()
        app.processEvents()


def test_main_preview_play_cancels_old_queued_first_frame_pause(monkeypatch) -> None:
    from sculptcut.ui import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    pauses, starts = [], []
    monkeypatch.setattr(window.player,"pause",lambda:pauses.append(1))
    monkeypatch.setattr(window.player,"play",lambda:starts.append(1))
    monkeypatch.setattr(window.player,"source",lambda:QUrl.fromLocalFile("C:/draft.mp4"))
    monkeypatch.setattr(window.player,"playbackState",
                        lambda:QMediaPlayer.PlaybackState.PausedState)

    class Frame:
        def toImage(self):
            return QImage(32,32,QImage.Format.Format_RGB32)

    try:
        window.pause_on_frame = True
        window.show_video_frame(Frame())
        window.play_pause()
        app.processEvents()
        assert starts == [1] and pauses == []
    finally:
        window._allow_close = True
        window.close()
        app.processEvents()


def test_immediate_play_source_switch_and_replay_at_end(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    paths = [tmp_path/"a.mp4",tmp_path/"b.mp4"]
    for path,color in zip(paths,("red","blue"),strict=True):
        run_process([discover("ffmpeg"),"-v","error","-f","lavfi","-i",
            f"color=c={color}:s=320x180:r=30:d=12","-c:v","libx264",str(path)])
    owner = QWidget()
    playback = EpisodePlayback(owner)
    playback.limit_frames = True
    received: list[tuple[QImage,int]] = []
    playback.image_ready.connect(lambda image,stamp:received.append((image,stamp)))
    try:
        for path,start,end in ((paths[0],3000,5000),(paths[1],7000,9000),
                               (paths[0],10000,12000),(paths[0],3000,5000)):
            playback.select(path,start,end,start)
            received.clear()
            playback.play()  # Do not wait for LoadedMedia or a seek acknowledgement.
            wait_until(app,lambda: bool(received))
            assert playback.want_play and not playback.awaiting_seek
            assert start <= received[-1][1] < end
            expected = received[-1][0].pixelColor(10,10)
            assert (expected.red()>200 if path == paths[0] else expected.blue()>200)
            playback.pause()
            playback.seek(end-1)
            wait_until(app,lambda: not playback.awaiting_seek)
            received.clear()
            playback.play()
            wait_until(app,lambda end=end: bool(received) and playback.player.position()<end-200)
            assert playback.want_play
            playback.pause()
    finally:
        playback.shutdown()
        owner.close()
        app.processEvents()
