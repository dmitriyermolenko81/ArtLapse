"""Music controls below the video lane; one clock, independent edit histories."""
import json
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QFileDialog

from sculptcut.export_storage import metadata_path
from sculptcut.i18n import tr
from sculptcut.media import probe
from sculptcut.music_edit import (
    LOCKED_MUSIC_REASON,
    MusicClip,
    assemble_music,
    draft_path,
    insert_music_clip,
)
from sculptcut.music_edit_dialog import MusicEditDialog, MusicJob
from sculptcut.music_library import audio_duration
from sculptcut.simple_timeline import SimpleTimeline
from sculptcut.track_edit import audible_clips

if TYPE_CHECKING:
    from sculptcut.source_selection_dialog import SourceSelectionDialog


class InlineMusic(MusicEditDialog):
    def __init__(self, owner: 'SourceSelectionDialog') -> None:
        self.owner = owner
        assert isinstance(owner.montage_seek, SimpleTimeline)
        self.video_lane = owner.montage_seek
        self.manual = False
        self.prepared_key: tuple[tuple[MusicClip, ...], int] | None = None
        super().__init__(owner.root, owner.video, owner, embedded=True)
        self.lane.compact_lane = True
        self.lane.show_ruler = False
        self.lane.left_padding = 56
        self.lane.lane_label = tr('Музыка')
        self.lane.setFixedHeight(40)
        self.music_header.hide()
        self.volume_state = owner.volume_state
        self.volume_state.changed.connect(self.monitor)
        self.manual = not self.locked and draft_path(self.root, self.video).is_file()
        metadata = metadata_path(self.video, '.music-edit.json')
        self.baseline = ([MusicClip.model_validate(c) for c in json.loads(
            metadata.read_text(encoding='utf-8'))] if not self.locked and metadata.is_file() else
            [MusicClip(path=self.video, start_us=0, end_us=self.duration, at_us=0)]
            if probe(self.video).audio_streams else [])
        self.lane.source_titles[self.video] = tr('Звук ролика')
        if self.locked:
            self.lane.source_titles[self.video] = tr('Под бит · только просмотр')
        if not self.manual:
            self.clips = list(self.baseline)
        self.refresh()
        self.lane.seek.disconnect(self.seek_music)
        self.player.positionChanged.disconnect(self.position)
        self.lane.seek.connect(self.navigate_lane)
        owner.cursor_changed.connect(self.follow_clock)
        owner.playback.playing_changed.connect(lambda _: self.sync_audio())
        owner.sequence.busy_changed.connect(lambda _: self.sync_audio())
        owner.plan_changed.connect(self.video_changed)
        self.video_lane.view_changed.connect(self.lane.set_view)
        self.lane.view_changed.connect(self.video_lane.set_view)
        self.video_changed()
        self.lane.set_view(self.video_lane.view_start, self.video_lane.view_span)
        self.player.errorOccurred.connect(lambda _, message: self.show_error(message))
        self.message.setText('')
        if self.locked:
            self.lane.setToolTip(tr(LOCKED_MUSIC_REASON))
        self.monitor()
        self.sync_timer = QTimer(self)
        self.sync_timer.setInterval(40)
        self.sync_timer.timeout.connect(self.sync_audio)
        self.sync_timer.start()

    def refresh(self, row: int = 0) -> None:
        super().refresh(row)
        self.options_button.hide()

    def navigate_lane(self, us: int) -> None:
        self.owner.seek_montage(us//1000)
        row = next((i for i, clip in enumerate(self.clips)
                    if clip.at_us <= us < clip.at_us+clip.end_us-clip.start_us), -1)
        if row != self.row:
            self.select(row)

    def add(self) -> None:
        if self.locked or self.worker:
            return
        self.owner.sequence.stop()
        self.owner.playback.pause()
        self.player.pause()
        at = self.lane.value()*1000
        path, _ = QFileDialog.getOpenFileName(self.owner.window(), tr('Добавить трек'), '',
            'Audio (*.mp3 *.wav *.flac *.m4a *.ogg *.aac *.opus *.wma)')
        if not path:
            return
        try:
            length = round(audio_duration(Path(path), None)*1e6)
            end = min(length, self.duration-at)
            if end < self.frame_us:
                raise ValueError(tr('Переместите курсор внутрь ролика.'))
            clip = MusicClip(path=Path(path), start_us=0, end_us=end, at_us=at)
            self.lane.source_lengths[clip.path] = length
            clips = insert_music_clip(self.clips, clip)
            row = next(i for i, c in enumerate(clips) if c.at_us == at)
            if clips[row].end_us-clips[row].start_us < self.frame_us:
                raise ValueError(tr('Переместите курсор внутрь ролика.'))
            self.commit(clips, row)
            if self.clips == clips:
                self.lane.setFocus()
                self.show_error(tr('Трек добавлен в свободный промежуток. '
                                   'Ctrl+Z — отмена.'))
        except (OSError, ValueError, RuntimeError) as exc:
            self.show_error(str(exc))

    @property
    def changed(self) -> bool:
        return self.clips != self.baseline

    @property
    def independent(self) -> bool:
        return self.manual or self.changed or (bool(self.clips) and self.owner.session.changed)

    def listen(self) -> None:
        self.owner.play_montage()

    def show_error(self, message: str) -> None:
        self.message.hide()
        self.owner.music_message.emit(tr(message))

    def commit(self, clips: list[MusicClip], row: int = 0) -> None:
        previous = list(self.clips)
        super().commit(clips, row)
        if self.clips != previous:
            self.manual = True
            self.invalidate()

    def replay(self, redo: bool) -> None:
        previous = list(self.clips)
        super().replay(redo)
        if self.clips != previous:
            self.manual = True
            self.invalidate()

    def invalidate(self) -> None:
        self.owner.sequence.stop()
        self.owner.playback.pause()
        self.prepared_key = None
        self.audition_ready = False
        self.player.stop()
        self.owner.apply.setEnabled(self.changed or self.owner.session.changed)
        self.message.hide()
        self.owner.refresh_audio()
        self.owner.music_changed.emit()

    def video_changed(self) -> None:
        self.duration = (self.owner.montage_seek.maximum()+1)*1000
        # Existing music remains anchored, including a tail outside a shorter video.
        self.lane.duration = max(self.duration,
            max((c.at_us+c.end_us-c.start_us for c in self.clips), default=0))
        self.lane.setRange(0, self.owner.montage_seek.maximum())
        self.player.pause()
        self.owner.apply.setEnabled(self.changed or self.owner.session.changed)
        self.monitor()

    def monitor(self) -> None:
        if not hasattr(self, 'baseline'):
            super().monitor()
            return
        self.audio.setVolume(self.volume_state.level/100)
        self.audio.setMuted(not self.independent or not self.audition_ready
                            or self.volume_state.silent)

    def prepare_playback(self, episode_only: bool) -> bool:
        if not self.independent:
            self.player.pause()
            return True
        key = (tuple(self.clips), self.duration)
        if key == self.prepared_key and self.audition_ready:
            return True
        if self.worker:
            return False
        clips, duration = audible_clips(self.clips, self.duration), self.duration
        self.owner.sequence.stop()
        self.owner.playback.pause()
        self.owner.setEnabled(False)
        self.show_error(tr('Готовим музыку…'))
        self.worker = MusicJob(lambda cancel: assemble_music(self.root, clips, duration, cancel))
        success = False

        def ready(path: str) -> None:
            nonlocal success
            self.player.setSource(QUrl.fromLocalFile(path))
            self.prepared_key = key
            self.audition_ready = True
            self.monitor()
            success = True

        def finished() -> None:
            if self.worker:
                self.worker.deleteLater()
                self.worker = None
            if self.owner.closed:
                return
            self.owner.setEnabled(True)
            if success:
                self.message.hide()
                self.owner.music_message.emit('')
                QTimer.singleShot(0, lambda: self.owner.play_montage(episode_only))

        self.worker.result.connect(ready)
        self.worker.error.connect(self.show_error)
        self.worker.finished.connect(finished)
        self.worker.start()
        return False

    def follow_clock(self, ms: int) -> None:
        self.position(ms)
        self.sync_audio()

    def sync_audio(self) -> None:
        if self.owner.closed or not self.independent or not self.audition_ready:
            self.player.pause()
            return
        ms = self.owner.montage_seek.value()
        playing = (self.owner.sequence.enabled and self.owner.sequence.desired
                   and not self.owner.sequence.waiting and self.owner.playback.is_advancing())
        running = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        if not playing:
            if running:
                self.player.pause()
            return
        # Qt position notifications are coarse and arrive on different clocks.
        # Seeking on every small difference repeats audio and flushes its buffer.
        if abs(self.player.position()-ms) > (250 if running else 80):
            self.pending_seek_ms = ms if not self.player.isSeekable() else None
            self.player.setPosition(ms)
        if playing and self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            self.player.play()

    def shutdown(self) -> None:
        self.wave_manager.shutdown()
        self.sync_timer.stop()
        if self.worker:
            self.worker.cancel.set()
            self.worker.wait()
            self.worker.deleteLater()
            self.worker = None
        self.seek_timer.stop()
        self.player.stop()
        self.player.setSource(QUrl())
        self.scratch.cleanup()
