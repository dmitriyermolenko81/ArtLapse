"""Bounded source playback for the episode editor; never changes edit decisions."""

from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame, QVideoSink


class EpisodePlayback(QObject):
    image_ready = Signal(QImage, int)
    position_changed = Signal(int)
    playing_changed = Signal(bool)
    failed = Signal(str)

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.5)
        self.player.setAudioOutput(self.audio)
        self.sink = QVideoSink(self)
        self.player.setVideoSink(self.sink)
        self.start = 0
        self.end = 0
        self.pending: int | None = None
        self.want_play = False
        self.loop = False
        self.closed = False
        self.timer = QTimer(self)
        self.timer.setInterval(20)
        self.timer.timeout.connect(self.check_end)
        self.player.positionChanged.connect(self.position_changed.emit)
        self.player.playbackStateChanged.connect(self.state_changed)
        self.player.mediaStatusChanged.connect(self.loaded)
        self.player.errorOccurred.connect(self.error)
        self.sink.videoFrameChanged.connect(self.frame_ready)

    def select(self, path: Path, start: int, end: int, position: int) -> None:
        self.pause()
        self.start, self.end = start, max(start + 1, end)
        url = QUrl.fromLocalFile(str(path.resolve()))
        position = max(self.start, min(position, self.end - 1))
        if self.player.source() != url:
            self.pending = position
            self.player.setSource(url)
        else:
            self.seek(position)

    def seek(self, position: int) -> None:
        if self.closed:
            return
        position = max(0, position)
        if self.player.mediaStatus() in (
            QMediaPlayer.MediaStatus.NoMedia, QMediaPlayer.MediaStatus.LoadingMedia,
        ):
            self.pending = position
        else:
            self.pending = None
            self.player.setPosition(position)
        self.position_changed.emit(position)

    def loaded(self, status: QMediaPlayer.MediaStatus) -> None:
        if self.closed:
            return
        if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
            if self.pending is not None:
                position, self.pending = self.pending, None
                self.player.setPosition(position)
            if self.want_play:
                self.player.play()
        elif status == QMediaPlayer.MediaStatus.EndOfMedia and self.want_play:
            self.finish()

    def play(self) -> None:
        if self.closed or self.end <= self.start or self.player.source().isEmpty():
            return
        position = self.pending if self.pending is not None else self.player.position()
        if not self.start <= position < self.end - 1:
            self.seek(self.start)
        self.want_play = True
        if self.pending is None:
            self.player.play()
        self.timer.start()

    def pause(self) -> None:
        self.want_play = False
        self.timer.stop()
        self.player.pause()

    def state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        self.playing_changed.emit(state == QMediaPlayer.PlaybackState.PlayingState)

    def check_end(self) -> None:
        if self.want_play and self.pending is None and self.player.position() >= self.end:
            self.finish()

    def finish(self) -> None:
        if self.loop:
            self.seek(self.start)
            self.player.play()
        else:
            self.pause()
            self.seek(self.end - 1)

    def frame_ready(self, frame: QVideoFrame) -> None:
        if self.closed or self.pending is not None or not frame.isValid():
            return
        image = frame.toImage()
        if not image.isNull():
            self.image_ready.emit(image, max(0, frame.startTime() // 1000))

    def error(self, error: QMediaPlayer.Error, message: str) -> None:
        self.pause()
        if not self.closed:
            self.failed.emit(message)

    def shutdown(self) -> None:
        self.closed = True
        self.pause()
        self.pending = None
        self.player.stop()
        self.player.setSource(QUrl())
