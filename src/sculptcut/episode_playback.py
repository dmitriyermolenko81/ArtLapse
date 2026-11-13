"""Bounded source playback for the episode editor; never changes edit decisions."""

from pathlib import Path
from time import monotonic

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QAudioOutput, QMediaMetaData, QMediaPlayer, QVideoFrame, QVideoSink


class EpisodePlayback(QObject):
    maximum_preview_fps = 30
    maximum_preview_size = 960
    image_ready = Signal(QImage, int)
    position_changed = Signal(int)
    playing_changed = Signal(bool)
    failed = Signal(str)
    finished = Signal()

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
        self.awaiting_seek = False
        self.seek_target = 0
        self.want_play = False
        self.loop = False
        self.closed = False
        self.limit_frames = False  # Source editor permits scrubbing beyond the selected episode.
        self.last_frame_time = 0.0
        self.timer = QTimer(self)
        self.timer.setInterval(20)
        self.timer.timeout.connect(self.check_end)
        self.bind_backend()

    def bind_backend(self) -> None:
        self.player.positionChanged.connect(self.position_changed.emit)
        self.player.playbackStateChanged.connect(self.state_changed)
        self.player.mediaStatusChanged.connect(self.loaded)
        self.player.errorOccurred.connect(self.error)
        sink = self.sink
        self.frame_callback = lambda frame: self.frame_ready(frame) if self.sink is sink else None
        self.sink.videoFrameChanged.connect(self.frame_callback, Qt.ConnectionType.QueuedConnection)

    def unbind_backend(self) -> None:
        self.player.positionChanged.disconnect(self.position_changed.emit)
        self.player.playbackStateChanged.disconnect(self.state_changed)
        self.player.mediaStatusChanged.disconnect(self.loaded)
        self.player.errorOccurred.disconnect(self.error)
        self.sink.videoFrameChanged.disconnect(self.frame_callback)

    def take_preloaded(self, other: "EpisodePlayback") -> None:
        """Keep the public playback object, adopt an already decoded paused backend."""
        volume, muted = self.audio.volume(), self.audio.isMuted()
        self.pause()
        other.pause()
        self.unbind_backend()
        other.unbind_backend()
        for name in ("player", "audio", "sink", "start", "end", "pending", "awaiting_seek",
                     "seek_target", "last_frame_time", "limit_frames"):
            left, right = getattr(self, name), getattr(other, name)
            setattr(self, name, right)
            setattr(other, name, left)
        self.bind_backend()
        other.bind_backend()
        self.audio.setVolume(volume)
        self.audio.setMuted(muted)
        other.audio.setMuted(True)
        # The paused sink already contains the next first frame. Publish it now,
        # instead of holding the old scene until the resumed decoder emits again.
        frame = self.sink.videoFrame()
        if frame.isValid():
            self.frame_ready(frame)

    def select(self, path: Path, start: int, end: int, position: int) -> None:
        self.pause()
        self.last_frame_time = 0
        self.start, self.end = start, max(start + 1, end)
        url = QUrl.fromLocalFile(str(path.resolve()))
        position = max(self.start, min(position, self.end - 1))
        self.awaiting_seek, self.seek_target = True, position
        if self.player.source() != url:
            self.pending = position
            self.player.setSource(url)
        else:
            self.seek(position)

    def seek(self, position: int) -> None:
        if self.closed:
            return
        position = max(0, position)
        self.awaiting_seek, self.seek_target = True, position
        self.last_frame_time = 0
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
                QTimer.singleShot(0, self.resume)
        elif (status == QMediaPlayer.MediaStatus.EndOfMedia and self.want_play
              and not self.awaiting_seek):
            self.finish()

    def play(self) -> None:
        if self.closed or self.end <= self.start or self.player.source().isEmpty():
            return
        self.want_play = True
        self.playing_changed.emit(True)
        position = self.pending if self.pending is not None else self.player.position()
        if not self.start <= position < self.end - 1:
            self.seek(self.start)
        QTimer.singleShot(0, self.resume)
        self.timer.start()

    def resume(self) -> None:
        if self.closed or not self.want_play or self.pending is not None:
            return
        if self.player.mediaStatus() in (
            QMediaPlayer.MediaStatus.NoMedia, QMediaPlayer.MediaStatus.LoadingMedia,
        ):
            return
        if self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            self.player.play()
            # EndOfMedia can reset position when play() starts. Apply the queued
            # source target after that reset, not before it.
            if self.awaiting_seek:
                self.player.setPosition(self.seek_target)

    def is_advancing(self) -> bool:
        """Audio may follow only a running decoder that has delivered a recent frame."""
        if (self.closed or not self.want_play or self.pending is not None or self.awaiting_seek
                or self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState
                or not self.last_frame_time):
            return False
        fps = self.player.metaData().value(QMediaMetaData.Key.VideoFrameRate)
        rate = max(0.01, self.player.playbackRate())
        tolerance = max(0.5, 2/(float(fps)*rate)) if fps else 0.5
        return monotonic()-self.last_frame_time <= tolerance

    def pause(self) -> None:
        was_playing = self.want_play
        self.want_play = False
        self.timer.stop()
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        if was_playing:
            self.playing_changed.emit(False)

    def state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        self.playing_changed.emit(self.want_play)

    def check_end(self) -> None:
        if (self.want_play and self.pending is None and not self.awaiting_seek
                and self.player.position() >= self.end):
            self.finish()

    def finish(self) -> None:
        if not self.want_play:
            return
        if self.loop:
            self.seek(self.start)
            self.player.play()
        else:
            self.pause()
            self.seek(self.end - 1)
            self.finished.emit()

    @Slot(QVideoFrame)
    def frame_ready(self, frame: QVideoFrame) -> None:
        if self.closed or self.pending is not None or not frame.isValid():
            return
        timestamp = frame.startTime() // 1000
        # Qt can deliver the old decoder's first frame after a queued seek.
        # A frame outside the selected source interval never belongs to it.
        if self.limit_frames and not self.start <= timestamp < self.end:
            return
        if self.awaiting_seek:
            fps = self.player.metaData().value(QMediaMetaData.Key.VideoFrameRate)
            tolerance = max(250, 1000/float(fps)) if fps else 250
            if abs(timestamp-self.seek_target) > tolerance:
                return
            self.awaiting_seek = False
        now = monotonic()
        fps = self.player.metaData().value(QMediaMetaData.Key.VideoFrameRate)
        incoming_rate = float(fps)*self.player.playbackRate() if fps else 0
        if (incoming_rate > self.maximum_preview_fps+0.5
                and now-self.last_frame_time < 0.9/self.maximum_preview_fps):
            return  # Accelerated 60fps sources must not flood image conversion/painting.
        self.last_frame_time = now
        image = frame.toImage()
        if not image.isNull():
            if max(image.width(), image.height()) > self.maximum_preview_size:
                image = image.scaled(self.maximum_preview_size, self.maximum_preview_size,
                                     Qt.AspectRatioMode.KeepAspectRatio)
            self.image_ready.emit(image, timestamp)

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
