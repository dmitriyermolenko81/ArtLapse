"""Experimental single-lane music editor with a frozen-video A/V audition."""
import tempfile
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path
from threading import Event

import numpy as np
from numpy.typing import NDArray
from PySide6.QtCore import QObject, QPoint, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QFocusEvent,
    QHideEvent,
    QImage,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame, QVideoSink
from PySide6.QtWidgets import (
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from sculptcut.i18n import tr
from sculptcut.lane_waveform import source_wave
from sculptcut.media import probe
from sculptcut.music_edit import (
    LOCKED_MUSIC_REASON,
    MusicClip,
    apply_music_edit,
    assemble_music,
    load_draft,
    music_edit_locked,
    music_gain,
    mux_music,
    save_draft,
)
from sculptcut.music_library import audio_duration
from sculptcut.playback_volume import PlaybackVolume, VolumeControl
from sculptcut.simple_timeline import SimpleTimeline
from sculptcut.time_format import format_time
from sculptcut.timeline_appearance import LanePiece, lane_bounds, paint_lane
from sculptcut.ui_theme import apply_dialog_theme


class WaveJob(QThread):
    ready = Signal(object, object, object)
    failed = Signal(str)

    def __init__(self, root: Path, paths: list[tuple[Path, tuple[int, int]]], parent: QObject):
        super().__init__(parent)
        self.root, self.paths = root, paths
        self.cancel = Event()

    def run(self) -> None:
        for path, identity in self.paths:
            if self.cancel.is_set():
                return
            try:
                wave = source_wave(self.root, path, self.cancel)
            except (OSError, ValueError, RuntimeError):
                wave = None
                if not self.cancel.is_set():
                    self.failed.emit(path.name)
            if not self.cancel.is_set():
                self.ready.emit(path, identity, wave)


class WaveManager(QObject):
    def __init__(self, root: Path, lane: 'MusicLane', parent: QObject):
        super().__init__(parent)
        self.root, self.lane = root, lane
        self.done: dict[Path, tuple[int, int]] = {}
        self.job: WaveJob | None = None
        self.stopped = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.prepare)

    def request(self) -> None:
        if self.stopped:
            return
        current = {c.path for c in self.lane.clips}
        for path in set(self.lane.waveforms)-current:
            self.lane.waveforms.pop(path, None)
        for path in set(self.done)-current:
            self.done.pop(path, None)
        if self.job is not None:
            if any(path not in current for path, _ in self.job.paths):
                self.job.cancel.set()
            return
        self.timer.start(150)

    def prepare(self) -> None:
        if self.stopped or self.job is not None:
            return
        needed = []
        for path in {c.path for c in self.lane.clips}:
            try:
                stat = path.stat()
            except OSError:
                self.lane.waveforms.pop(path, None)
                continue
            identity = (stat.st_size, stat.st_mtime_ns)
            if self.done.get(path) != identity:
                self.lane.waveforms.pop(path, None)
                needed.append((path, identity))
        if not needed:
            return
        job = WaveJob(self.root, needed, self)
        self.job = job
        job.ready.connect(self.accept_wave)
        owner = self.parent()
        if isinstance(owner, MusicEditDialog):
            job.failed.connect(lambda name: owner.show_error(
                tr('Не удалось построить звуковую волну: {name}').format(name=name)))
        job.finished.connect(self.finished)
        job.start()

    def accept_wave(self, path: Path, identity: tuple[int, int],
                    wave: tuple[float, NDArray[np.float32]] | None) -> None:
        if self.stopped or path not in {c.path for c in self.lane.clips}:
            return
        try:
            stat = path.stat()
        except OSError:
            return
        if (stat.st_size, stat.st_mtime_ns) != identity:
            return
        self.done[path] = identity
        if wave is not None:
            self.lane.waveforms[path] = wave
        self.lane.update()

    def finished(self) -> None:
        job, self.job = self.job, None
        if job is not None:
            job.deleteLater()
        self.request()

    def shutdown(self) -> None:
        self.stopped = True
        self.timer.stop()
        if self.job is not None:
            self.job.cancel.set()
            self.job.wait()
            self.job.deleteLater()
            self.job = None


class MusicLane(SimpleTimeline):
    selected = Signal(int)
    edited = Signal(int, str, int)
    seek = Signal(int)
    add_requested = Signal()
    details_requested = Signal()
    remove_requested = Signal()
    context_requested = Signal(QPoint)

    def __init__(self, duration: int) -> None:
        super().__init__()
        self.duration = duration
        self.setRange(0, round(duration/1000))
        self.clips: list[MusicClip] = []
        self.row = -1
        self.read_only = False
        self.playhead_us = 0
        self.drag: tuple[int, str, int] | None = None
        self.delta = 0
        self.press_position = QPoint()
        self.waveforms: dict[Path, tuple[float, NDArray[np.float32]]] = {}
        self.source_lengths: dict[Path, int] = {}
        self.source_titles: dict[Path, str] = {}
        self.setFixedHeight(104)
        self.setStyleSheet('')
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(tr('Музыкальная дорожка'))
        self.setMouseTracking(True)
        self.sliderMoved.connect(lambda ms: self.seek.emit(ms*1000))
        self.remove_scene.connect(self.remove_requested.emit)
        self.navigation_tip = tr('Пробел — слушать / пауза\nQ / Й — разделить\n'
            'Delete / Backspace — удалить\nCtrl+Z — отменить\nCtrl+Y / Ctrl+Shift+Z — повторить\n'
            '← / → — кадр; Shift — 10 кадров\n↑ / ↓ — предыдущий / следующий фрагмент\n'
            'Home / End — начало / конец\nF / \\ — вся дорожка\n'
            'Колесо — прокрутка; Ctrl+колесо — масштаб\nСредняя кнопка — сдвинуть шкалу')
        self.setToolTip(self.navigation_tip)

    @property
    def playhead_us(self) -> int:
        return self.value()*1000

    @playhead_us.setter
    def playhead_us(self, value: int) -> None:
        self.setValue(round(value/1000))

    def bounded_delta(self, row: int, mode: str, delta: int) -> int:
        clip = self.clips[row]
        length = clip.end_us-clip.start_us
        end = clip.at_us+length
        before = max((c.at_us+c.end_us-c.start_us for i, c in enumerate(self.clips)
                      if i != row and c.at_us < clip.at_us), default=0)
        after = min((c.at_us for i, c in enumerate(self.clips)
                     if i != row and c.at_us >= end), default=self.duration)
        minimum = min(10_000, length)
        if mode in ('fade_in', 'fade_out'):
            current = self.fade_duration(clip, mode)
            other = self.fade_duration(clip, 'fade_out' if mode == 'fade_in' else 'fade_in')
            maximum = max(0, length-other)
            return max(-current, min(maximum-current, delta)) if mode == 'fade_in' else (
                max(current-maximum, min(current, delta)))
        if mode == 'move':
            low, high = before-clip.at_us, after-end
        elif mode == 'left':
            low, high = max(-clip.start_us, before-clip.at_us), length-minimum
        else:
            low, high = minimum-length, after-end
            if clip.path in self.source_lengths:
                high = min(high, self.source_lengths[clip.path]-clip.end_us)
        return max(low, min(high, delta))

    def paintEvent(self, event: QPaintEvent) -> None:
        pieces = []
        for i, clip in enumerate(self.clips):
            at, length = clip.at_us, clip.end_us-clip.start_us
            if self.drag and self.drag[0] == i:
                mode = self.drag[1]
                at += self.delta if mode in ('move', 'left') else 0
                length += -self.delta if mode == 'left' else (
                    self.delta if mode == 'right' else 0)
            pieces.append(LanePiece(i, at/1000, (at+length)/1000,
                self.source_titles.get(clip.path, clip.path.stem),
                f'{format_time(clip.start_us/1e6, 2)} – {format_time(clip.end_us/1e6, 2)}'))
        p = QPainter(self)
        paint_lane(p, self, pieces, self.row, tr('+ Добавить музыку'), self.paint_wave)
        top, bottom = lane_bounds(self)
        for i, clip in enumerate(self.clips):
            if not any(self.fade_active(clip, mode) for mode in ('fade_in', 'fade_out')):
                continue
            if (self.x_at((clip.at_us+clip.end_us-clip.start_us)/1000)
                    -self.x_at(clip.at_us/1000)) < 28:
                continue
            shown = clip
            if self.drag and self.drag[0] == i and self.drag[1] in ('fade_in', 'fade_out'):
                mode = self.drag[1]
                value = self.fade_duration(clip, mode)+(self.delta if mode == 'fade_in'
                         else -self.delta)
                shown = clip.model_copy(update={mode+'_us': value,
                    'envelope_start_us': (clip.start_us if mode == 'fade_in'
                        else clip.envelope_start_us),
                    'envelope_end_us': (clip.end_us if mode == 'fade_out'
                        else clip.envelope_end_us)})
            start = (shown.envelope_start_us if shown.envelope_start_us is not None
                     else shown.start_us)
            end = shown.envelope_end_us if shown.envelope_end_us is not None else shown.end_us
            points = sorted({shown.start_us, shown.end_us,
                max(shown.start_us, min(shown.end_us, start+shown.fade_in_us)),
                max(shown.start_us, min(shown.end_us, end-shown.fade_out_us))})
            p.setPen(QPen(QColor('#8ce4ef'), 1.5))
            previous = None
            for source in points:
                point = QPoint(round(self.x_at((shown.at_us+source-shown.start_us)/1000)),
                    round(bottom-3-(bottom-top-7)*music_gain(shown, source)
                          /max(shown.volume, .001)))
                if previous is not None:
                    p.drawLine(previous, point)
                previous = point
            if not self.read_only:
                p.setBrush(QColor('#8ce4ef'))
                for mode in ('fade_in', 'fade_out'):
                    if not self.fade_active(shown, mode):
                        continue
                    p.drawEllipse(QPoint(round(self.fade_x(shown, mode)), top+5), 4, 4)
        for ms in self.split_positions:
            x = round(self.x_at(ms))
            p.fillRect(x-3, top, 7, bottom-top, QColor('#111923'))
            p.fillRect(x-1, top, 3, bottom-top, QColor('#73d8e8'))

    def paint_wave(self, p: QPainter, piece: LanePiece, x: int, width: int,
                   top: int, bottom: int) -> None:
        wave = self.waveforms.get(self.clips[piece.row].path)
        if wave is None or width < 4:
            return
        step, levels = wave
        clip = self.clips[piece.row]
        source_start = clip.start_us
        if self.drag and self.drag[0] == piece.row and self.drag[1] == 'left':
            source_start += self.delta
        left = max(x+3, self.left_padding if self.compact_lane else 1)
        right = min(x+width-3, self.width()-10)
        middle = (top+bottom)/2
        height = max(1., (bottom-top-8)/2)
        p.save()
        p.setClipRect(max(x+2, left), top+2, max(0, right-left), bottom-top-3)
        p.setPen(QPen(QColor(143, 207, 194, 105), 1))
        for px in range(left, right, 3):
            start_ms = self.view_start+(px-self.left_padding)*self.view_span / max(
                1, self.width()-self.left_padding-10)
            end_ms = self.view_start+(px+3-self.left_padding)*self.view_span / max(
                1, self.width()-self.left_padding-10)
            a = max(0, int((source_start/1e6+(start_ms-piece.start_ms)/1000)/step))
            b = min(len(levels), max(a+1, int(
                (source_start/1e6+(end_ms-piece.start_ms)/1000)/step)+1))
            if a >= len(levels) or b <= a:
                continue
            amplitude = min(1., float(max(levels[a:b])))
            gain = music_gain(clip, source_start+round((start_ms-piece.start_ms)*1000))
            h = round(height*amplitude**0.5*gain)
            if h:
                p.drawLine(px, round(middle-h), px, round(middle+h))
        p.restore()

    @classmethod
    def fade_active(cls, clip: MusicClip, mode: str) -> bool:
        return getattr(clip, mode+'_us') > 0 and cls.fade_duration(clip, mode) > 0

    @staticmethod
    def fade_duration(clip: MusicClip, mode: str) -> int:
        start = clip.envelope_start_us if clip.envelope_start_us is not None else clip.start_us
        end = clip.envelope_end_us if clip.envelope_end_us is not None else clip.end_us
        value = (start+clip.fade_in_us-clip.start_us if mode == 'fade_in'
                 else clip.end_us-end+clip.fade_out_us)
        return max(0, min(clip.end_us-clip.start_us, value))

    def fade_x(self, clip: MusicClip, mode: str) -> float:
        length = clip.end_us-clip.start_us
        start = clip.envelope_start_us if clip.envelope_start_us is not None else clip.start_us
        end = clip.envelope_end_us if clip.envelope_end_us is not None else clip.end_us
        t = (start+clip.fade_in_us if mode == 'fade_in' else end-clip.fade_out_us)-clip.start_us
        x = self.x_at((clip.at_us+t)/1000)
        return max(self.x_at(clip.at_us/1000)+5,
                   min(self.x_at((clip.at_us+length)/1000)-5, x))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self.compact_lane and event.position().x() < self.left_padding-8:
            self.setFocus()
            return
        if event.button() == Qt.MouseButton.MiddleButton:
            super().mousePressEvent(event)
            return
        if self.pan_x is not None or self.pointer_down:
            return
        if event.button() == Qt.MouseButton.RightButton:
            self.setFocus()
            self.navigate(self.time_at(event.position().x()))
            if not self.read_only:
                self.context_requested.emit(event.globalPosition().toPoint())
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus()
        if self.read_only:
            self.navigate(self.time_at(event.position().x()))
            return
        top, bottom = lane_bounds(self)
        if not self.clips and top <= event.position().y() <= bottom:
            self.add_requested.emit()
            return
        self.pointer_down = True
        t = self.time_at(event.position().x())*1000
        for i, c in enumerate(self.clips):
            end = c.at_us+c.end_us-c.start_us
            if c.at_us <= t <= end and top-3 <= event.position().y() <= bottom:
                self.selected.emit(i)
                a = self.x_at(c.at_us/1000)
                b = self.x_at(end/1000)
                for fade in ('fade_in', 'fade_out'):
                    if (self.fade_active(c, fade) and b-a >= 28
                            and abs(event.position().y()-(top+5)) <= 6
                            and abs(event.position().x()-self.fade_x(c, fade)) <= 7):
                        self.drag = (i, fade, t)
                        self.press_position = event.position().toPoint()
                        self.delta = 0
                        return
                mode = 'left' if abs(event.position().x()-a)<8 else (
                    'right' if abs(event.position().x()-b)<8 else 'move')
                self.drag = (i, mode, t)
                self.press_position = event.position().toPoint()
                self.delta = 0
                if mode == 'move':
                    self.navigate(t//1000)
                return
        self.navigate(t//1000)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.pan_x is not None:
            super().mouseMoveEvent(event)
            return
        if self.drag:
            self.delta = self.bounded_delta(self.drag[0], self.drag[1],
                self.time_at(event.position().x())*1000-self.drag[2])
            self.update()
            return
        if self.pointer_down:
            self.navigate(self.time_at(event.position().x()))
            return
        t = self.time_at(event.position().x())*1000
        cursor = Qt.CursorShape.PointingHandCursor if not self.clips else Qt.CursorShape.CrossCursor
        hint = tr('Нажмите на шкалу, чтобы перейти к нужному моменту.')
        for clip in self.clips:
            end = clip.at_us+clip.end_us-clip.start_us
            top, bottom = lane_bounds(self)
            if clip.at_us <= t <= end and top <= event.position().y() <= bottom:
                edge = min(abs(event.position().x()-self.x_at(clip.at_us/1000)),
                           abs(event.position().x()-self.x_at(end/1000))) < 8
                cursor = Qt.CursorShape.SizeHorCursor if edge else Qt.CursorShape.OpenHandCursor
                hint = tr('Тяните край, чтобы подрезать.') if edge else tr(
                    'Тяните фрагмент, чтобы переместить.')
                if abs(event.position().y()-(top+5)) <= 6:
                    for mode in ('fade_in', 'fade_out'):
                        if (self.fade_active(clip, mode)
                                and abs(event.position().x()-self.fade_x(clip, mode)) <= 7):
                            cursor = Qt.CursorShape.SizeHorCursor
                            hint = tr('Плавное нарастание громкости') if mode == 'fade_in' else tr(
                                'Плавное затухание громкости')
                break
        self.setCursor(Qt.CursorShape.CrossCursor if self.read_only else cursor)
        self.setToolTip(tr(LOCKED_MUSIC_REASON) if self.read_only
                       else hint+'\n\n'+self.navigation_tip)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            super().mouseReleaseEvent(event)
            return
        if event.button() != Qt.MouseButton.LeftButton or not self.pointer_down:
            return
        self.pointer_down = False
        if self.drag:
            row, mode, start = self.drag
            end = self.time_at(event.position().x())*1000
            delta = self.bounded_delta(row, mode, end-start)
            if delta and (event.position().toPoint()-self.press_position).manhattanLength() >= 6:
                self.edited.emit(row, mode, delta)
            self.drag = None
            self.delta = 0
            self.update()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        self.cancel_drag()
        if not self.read_only and event.button() == Qt.MouseButton.LeftButton and self.row >= 0:
            self.details_requested.emit()

    def cancel_drag(self) -> None:
        self.drag = None
        self.delta = 0
        self.pointer_down = False
        self.update()

    def focusOutEvent(self, event: QFocusEvent) -> None:
        self.cancel_drag()
        super().focusOutEvent(event)

    def hideEvent(self, event: QHideEvent) -> None:
        self.cancel_drag()
        super().hideEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self.read_only and event.key() in (Qt.Key.Key_Q, ord('Й'), Qt.Key.Key_Delete,
                Qt.Key.Key_Backspace, Qt.Key.Key_Z, Qt.Key.Key_Y,
                Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape and self.drag:
            self.drag = None
            self.delta = 0
            self.pointer_down = False
            self.update()
            event.accept()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.clips:
                self.details_requested.emit()
            else:
                self.add_requested.emit()
        else:
            super().keyPressEvent(event)


class MusicJob(QThread):
    result = Signal(str)
    error = Signal(str)

    def __init__(self, operation: Callable[[Event], Path]) -> None:
        super().__init__()
        self.operation = operation
        self.cancel = Event()

    def run(self) -> None:
        try:
            self.result.emit(str(self.operation(self.cancel)))
        except (OSError, ValueError, RuntimeError) as exc:
            self.error.emit(str(exc))


class MusicEditDialog(QDialog):
    version_saved = Signal(str)
    closed = Signal(str)

    def __init__(self, root: Path, video: Path, parent: QWidget, *, embedded: bool = False) -> None:
        super().__init__(parent)
        self.embedded = embedded
        apply_dialog_theme(self)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowTitle(tr('Музыка к ролику'))
        self.resize(900, 660)
        self.root, self.video = root, video
        self.locked = music_edit_locked(video)
        info = probe(video)
        self.duration = info.duration_us or 0
        self.frame_us = Fraction(1_000_000*info.fps_den, info.fps_num) if (
            info.fps_num > 0 and info.fps_den > 0) else Fraction(1_000_000, 30)
        if not self.duration:
            raise ValueError('Не удалось определить длительность видео.')
        self.clips = ([MusicClip(path=video, start_us=0, end_us=self.duration, at_us=0)]
            if self.locked and info.audio_streams
            else [] if self.locked else load_draft(root, video))
        self.undo_stack: list[list[MusicClip]] = []
        self.redo_stack: list[list[MusicClip]] = []
        self.worker: MusicJob | None = None
        self.scratch = tempfile.TemporaryDirectory(prefix='artlapse-music-',
                                                    ignore_cleanup_errors=True)
        self.serial = 0
        self.saved_path = ''
        self.pending_seek_ms: int | None = None
        self.row = -1
        self.poster_pending = True
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)
        title = QLabel(tr('Музыка к ролику'))
        title.setObjectName('sectionTitle')
        layout.addWidget(title)
        self.picture = QLabel()
        self.picture.setObjectName('introPreview')
        self.picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.picture.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.picture.setMinimumHeight(240)
        layout.addWidget(self.picture, 1)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.frame_image = QImage()
        self.sink = QVideoSink(self)
        self.sink.videoFrameChanged.connect(self.show_frame)
        self.player.setVideoSink(self.sink)
        if not embedded:
            self.player.setSource(QUrl.fromLocalFile(str(video)))
        self.volume_state = getattr(parent, 'volume_state', None) or PlaybackVolume(parent=self)
        self.audition_ready = False
        self.volume_state.changed.connect(self.monitor)
        self.monitor()
        transport = QHBoxLayout()
        self.listen_button = QPushButton(tr('▶ Слушать'))
        self.listen_button.setMinimumWidth(130)
        self.listen_button.clicked.connect(self.listen)
        transport.addWidget(self.listen_button)
        self.time_label = QLabel()
        self.time_label.setObjectName('playbackTime')
        transport.addWidget(self.time_label)
        transport.addStretch()
        self.monitor_control = VolumeControl(self.volume_state)
        transport.addWidget(self.monitor_control)
        layout.addLayout(transport)
        self.music_header = QWidget()
        music_header = QHBoxLayout(self.music_header)
        music_header.setContentsMargins(0, 0, 0, 0)
        self.add_button = QPushButton(tr('+ Добавить музыку'))
        self.add_button.clicked.connect(self.add)
        music_header.addWidget(self.add_button)
        music_header.addStretch()
        self.undo_button = QPushButton('↶')
        self.undo_button.setFixedWidth(36)
        self.undo_button.setToolTip(tr('Отменить правку (Ctrl+Z)'))
        self.undo_button.setAccessibleName(tr('Отменить правку'))
        self.undo_button.clicked.connect(self.undo)
        music_header.addWidget(self.undo_button)
        for key, action in (('Ctrl+Z', lambda: self.keyboard_history(False)),
                            ('Ctrl+Y', lambda: self.keyboard_history(True)),
                            ('Ctrl+Shift+Z', lambda: self.keyboard_history(True)),
                            ('Space', self.keyboard_listen)):
            shortcut = QShortcut(QKeySequence(key), self)
            if embedded:
                shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.setAutoRepeat(False)
            shortcut.activated.connect(action)
        self.options_button = QPushButton('•••')
        self.options_button.setFixedWidth(42)
        self.options_button.setToolTip(tr('Плавность музыки'))
        self.options_button.setAccessibleName(tr('Настроить или удалить фрагмент'))
        options = QMenu(self.options_button)
        self.details_action = options.addAction(tr('Подрезать точно…'), self.show_details)
        self.remove_action = options.addAction(tr('Удалить фрагмент'), self.remove)
        options.addSeparator()
        self.split_action = options.addAction(tr('Разделить здесь (Q)'), self.split)
        self.redo_action = options.addAction(tr('Повторить правку (Ctrl+Y)'), self.redo)
        # Keep legacy action objects for internal callers, but simplify the menu.
        for menu_action in list(options.actions()):
            options.removeAction(menu_action)
        self.add_track_action = options.addAction(tr('Добавить трек'), self.add)
        self.fade_in_action = options.addAction(tr('Добавить плавное начало'),
            lambda: self.toggle_fade('fade_in'))
        self.fade_out_action = options.addAction(tr('Добавить затухание'),
            lambda: self.toggle_fade('fade_out'))
        options.aboutToShow.connect(self.update_fade_actions)
        self.options_menu = options
        self.options_button.clicked.connect(lambda: options.popup(
            self.options_button.mapToGlobal(self.options_button.rect().bottomLeft())))
        music_header.addWidget(self.options_button)
        layout.addWidget(self.music_header)
        self.lane = MusicLane(self.duration)
        self.lane.read_only = self.locked
        self.lane.frame_ms = float(self.frame_us/1000)
        layout.addWidget(self.lane)
        self.player.positionChanged.connect(self.position)
        self.player.mediaStatusChanged.connect(self.media_status)
        self.seek_timer = QTimer(self)
        self.seek_timer.setSingleShot(True)
        self.seek_timer.timeout.connect(self.finish_seek)
        self.lane.seek.connect(self.seek_music)
        self.lane.selected.connect(self.select)
        self.lane.edited.connect(self.drag_edit)
        self.lane.add_requested.connect(self.add)
        self.lane.details_requested.connect(self.show_details)
        self.lane.remove_requested.connect(self.remove)
        self.lane.split_scene.connect(self.split)
        self.lane.play_requested.connect(self.listen)
        self.lane.undo_requested.connect(self.undo)
        self.lane.redo_requested.connect(self.redo)
        self.lane.context_requested.connect(self.options_menu.popup)
        self.hint = QLabel(tr('Пробел — слушать · Q — разделить · Delete — удалить'))
        self.hint.setObjectName('sectionHint')
        self.hint.setWordWrap(True)
        self.hint.setToolTip(self.lane.navigation_tip)
        layout.addWidget(self.hint)
        self.details = QWidget()
        fields = QGridLayout(self.details)
        fields.setContentsMargins(0, 0, 0, 0)
        self.at, self.start, self.end, self.volume = (
            QDoubleSpinBox() for _ in range(4))
        for i, (label, box) in enumerate(zip(
                ('В ролике, с', 'Начало трека, с', 'Конец трека, с', 'Громкость, %'),
                (self.at, self.start, self.end, self.volume), strict=True)):
            box.setRange(0, 1_000_000)
            box.setDecimals(3)
            box.setKeyboardTracking(False)
            box.editingFinished.connect(self.change)
            fields.addWidget(QLabel(tr(label)), i//2, i%2*2)
            fields.addWidget(box, i//2, i%2*2+1)
        self.volume.setRange(0, 200)
        self.hide_details_button = QPushButton(tr('Готово'))
        self.hide_details_button.clicked.connect(self.details.hide)
        fields.addWidget(self.hide_details_button, 2, 3)
        layout.addWidget(self.details)
        self.details.hide()
        self.message = QLabel()
        self.message.setObjectName('sectionHint')
        self.message.setWordWrap(True)
        status = QHBoxLayout()
        status.addWidget(self.message, 1)
        self.save_button = QPushButton(tr('Сохранить видео'))
        self.save_button.setObjectName('primary')
        self.save_button.setToolTip(tr('Создать копию ролика с этой музыкой'))
        self.save_button.clicked.connect(self.apply)
        status.addWidget(self.save_button)
        layout.addLayout(status)
        self.edit_widgets: list[QWidget] = [self.lane, self.details, self.add_button,
            self.undo_button, self.options_button, self.listen_button, self.save_button]
        self.wave_manager = WaveManager(self.root, self.lane, self)
        self.refresh()
        if self.locked:
            self.lane.setToolTip(tr(LOCKED_MUSIC_REASON))
            self.message.setText(tr(LOCKED_MUSIC_REASON))
            self.audition_ready = True
            self.monitor()
        self.position(0)
        for button in (self.listen_button, self.add_button, self.undo_button,
                       self.options_button, self.hide_details_button, self.save_button,
                       self.monitor_control.mute_button):
            button.setAutoDefault(False)
        self.player.playbackStateChanged.connect(self.play_state)
        if embedded:
            self.setObjectName('inlineMusic')
            self.setStyleSheet(self.styleSheet()+'\nQDialog#inlineMusic {background:#181e26;}')
            self.setWindowFlags(Qt.WindowType.Widget)
            self.setWindowModality(Qt.WindowModality.NonModal)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(2)
            for widget in (title, self.picture, self.listen_button, self.time_label,
                           self.monitor_control, self.hint, self.save_button):
                widget.hide()
            music_header.insertWidget(0, QLabel(tr('Музыка')))
            self.message.hide()
        else:
            self.player.play()

    def listen(self) -> None:
        if not self.worker and self.clips:
            if not self.audition_ready:
                self.preview()
            else:
                self.toggle_play()

    def editing_fields(self) -> bool:
        focus = self.focusWidget()
        return focus is not None and self.details.isAncestorOf(focus)

    def keyboard_listen(self) -> None:
        if not self.editing_fields() and not self.lane.pointer_down and self.lane.pan_x is None:
            self.listen()

    def keyboard_history(self, redo: bool) -> None:
        if not self.editing_fields() and not self.lane.pointer_down and self.lane.pan_x is None:
            self.replay(redo)

    def seek_music(self, us: int) -> None:
        if self.worker:
            return
        ms = round(max(0, min(self.duration, us))/1000)
        self.position(ms)
        row = next((i for i, c in enumerate(self.clips)
                    if c.at_us <= ms*1000 < c.at_us+c.end_us-c.start_us), -1)
        if row != self.row:
            self.select(row)
        if not self.player.isSeekable():
            self.pending_seek_ms = ms
        self.player.setPosition(ms)

    def show_details(self) -> None:
        if not self.locked and self.row >= 0 and not self.worker:
            self.details.show()
            self.start.setFocus()

    def show_error(self, message: str) -> None:
        self.message.setText(tr(message))
        self.message.setVisible(bool(message))

    def toggle_play(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        elif self.audition_ready and not self.worker:
            if self.player.position() >= self.duration//1000-50:
                self.player.setPosition(0)
            self.player.play()

    def play_state(self, state: QMediaPlayer.PlaybackState) -> None:
        self.listen_button.setText(tr('Ⅱ Пауза' if self.audition_ready and
            state == QMediaPlayer.PlaybackState.PlayingState else '▶ Слушать'))

    def media_status(self, status: QMediaPlayer.MediaStatus) -> None:
        if self.pending_seek_ms is not None and status in (
                QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
            self.seek_timer.start(0)

    def finish_seek(self) -> None:
        if self.pending_seek_ms is not None:
            position, self.pending_seek_ms = self.pending_seek_ms, None
            self.player.setPosition(position)

    def monitor(self) -> None:
        self.audio.setVolume(self.volume_state.level/100)
        self.audio.setMuted(not self.audition_ready or self.volume_state.silent)

    def show_frame(self, frame: QVideoFrame) -> None:
        image = frame.toImage()
        if not image.isNull():
            self.frame_image = image
            self.picture.setPixmap(QPixmap.fromImage(image).scaled(self.picture.size(),
                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            if self.poster_pending:
                self.poster_pending = False
                QTimer.singleShot(0, self.pause_poster)

    def pause_poster(self) -> None:
        if (not self.audition_ready or self.locked) and not self.worker:
            self.player.pause()

    def position(self, ms: int) -> None:
        self.lane.playhead_us = ms*1000
        if (not self.lane.pointer_down and self.lane.pan_x is None
                and self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
                and not self.lane.view_start <= ms <= self.lane.view_start+self.lane.view_span):
            self.lane.set_view(ms-self.lane.view_span/2, self.lane.view_span)
        self.lane.update()
        self.time_label.setText(
            f'{format_time(ms/1000, 2)} / {format_time(self.duration/1e6, 2)}')

    def refresh(self, row: int = 0) -> None:
        self.lane.clips = self.clips
        self.wave_manager.request()
        self.lane.blocks = [(i, round(c.at_us/1000),
            round((c.at_us+c.end_us-c.start_us)/1000)) for i, c in enumerate(self.clips)]
        ordered = sorted(self.clips, key=lambda c: c.at_us)
        self.lane.split_positions = [round(right.at_us/1000)
            for left, right in zip(ordered, ordered[1:], strict=False)
            if left.path == right.path and left.volume == right.volume
            and left.end_us == right.start_us
            and left.at_us+left.end_us-left.start_us == right.at_us]
        self.select(min(row, len(self.clips)-1))
        self.undo_button.setEnabled(bool(self.undo_stack) and not self.worker)
        self.listen_button.setEnabled(bool(self.clips) and not self.worker)
        self.save_button.setEnabled(bool(self.clips) and not self.worker)
        self.hint.setVisible(bool(self.clips) and not self.embedded)
        self.add_button.setVisible(bool(self.clips))
        self.options_button.setVisible(self.embedded or bool(self.clips or self.redo_stack))
        self.undo_button.setVisible(bool(self.undo_stack or self.redo_stack))
        self.add_button.setEnabled(not self.locked and not self.worker)
        if self.locked:
            self.options_button.setVisible(True)
            self.options_button.setEnabled(False)
            self.undo_button.setEnabled(False)
            self.save_button.setEnabled(False)

    def select(self, row: int) -> None:
        self.row = row
        self.lane.row = row
        self.lane.selected_row = row
        self.lane.update()
        selected = 0 <= row < len(self.clips) and not self.worker and not self.locked
        self.options_button.setEnabled(not self.locked and not self.worker
            and (self.embedded or selected or bool(self.redo_stack)))
        for action in (self.details_action, self.remove_action, self.split_action):
            action.setEnabled(selected)
        self.redo_action.setEnabled(bool(self.redo_stack) and not self.worker and not self.locked)
        if row < 0:
            self.details.hide()
        if 0 <= row < len(self.clips):
            c = self.clips[row]
            for box, value in zip((self.at, self.start, self.end, self.volume),
                                   (c.at_us/1e6, c.start_us/1e6, c.end_us/1e6, c.volume*100),
                                   strict=True):
                box.setValue(value)

    def commit(self, clips: list[MusicClip], row: int = 0) -> None:
        if self.worker or self.locked:
            return
        if clips == self.clips:
            return
        from sculptcut.music_edit import validate_clips
        try:
            if clips:
                validate_clips(clips, max(self.duration, self.lane.duration)
                               if self.embedded else self.duration)
            save_draft(self.root, self.video, clips)
            self.undo_stack.append(self.clips)
            self.redo_stack.clear()
            self.clips = clips
            self.player.pause()
            self.audition_ready = False
            self.monitor()
            self.refresh(row)
            self.play_state(self.player.playbackState())
            self.message.setText(tr('Правки сохранены. Нажмите «Слушать», чтобы проверить.'))
        except (OSError, ValueError) as exc:
            self.show_error(str(exc))

    def add(self) -> None:
        if self.locked or self.worker:
            return
        path, _ = QFileDialog.getOpenFileName(self, tr('Добавить трек'), '',
            'Audio (*.mp3 *.wav *.flac *.m4a *.ogg *.aac *.opus *.wma)')
        if not path:
            return
        try:
            length = round(audio_duration(Path(path), None)*1e6)
            self.lane.source_lengths[Path(path)] = length
            at = max((c.at_us+c.end_us-c.start_us for c in self.clips), default=0)
            end = min(length, self.duration-at)
            if end <= 0:
                raise ValueError(tr('На дорожке нет места. Сначала укоротите фрагмент.'))
            self.commit([*self.clips, MusicClip(path=Path(path), start_us=0, end_us=end,
                                               at_us=at)], len(self.clips))
        except (OSError, ValueError, RuntimeError) as exc:
            self.show_error(str(exc))

    def change(self) -> None:
        if self.worker or self.locked:
            return
        row = self.row
        if 0 <= row < len(self.clips):
            try:
                c = MusicClip.model_validate(self.clips[row].model_dump() | dict(
                    at_us=round(self.at.value()*1e6),
                    start_us=round(self.start.value()*1e6), end_us=round(self.end.value()*1e6),
                    volume=self.volume.value()/100))
                old = self.clips[row]
                if (c.start_us, c.end_us) != (old.start_us, old.end_us):
                    length = c.end_us-c.start_us
                    c = c.model_copy(update={'envelope_start_us': c.start_us,
                        'envelope_end_us': c.end_us, 'fade_in_us': min(c.fade_in_us, length),
                        'fade_out_us': min(c.fade_out_us, max(0, length-c.fade_in_us))})
                if c.path not in self.lane.source_lengths:
                    self.lane.source_lengths[c.path] = round(audio_duration(c.path, None)*1e6)
                if c.end_us > self.lane.source_lengths[c.path]:
                    raise ValueError(tr('Конец фрагмента выходит за конец трека.'))
                self.commit([*self.clips[:row], c, *self.clips[row+1:]], row)
            except (OSError, ValueError, RuntimeError) as exc:
                self.show_error(str(exc))

    def update_fade_actions(self) -> None:
        selected = 0 <= self.row < len(self.clips) and not self.locked and not self.worker
        self.add_track_action.setEnabled(not self.locked and not self.worker)
        for mode, action, add, remove in (
            ('fade_in', self.fade_in_action, 'Добавить плавное начало', 'Убрать плавное начало'),
            ('fade_out', self.fade_out_action, 'Добавить затухание', 'Убрать затухание'),
        ):
            active = selected and self.lane.fade_active(self.clips[self.row], mode)
            action.setText(tr(remove if active else add))
            available = False
            if selected:
                clip = self.clips[self.row]
                other = 'fade_out' if mode == 'fade_in' else 'fade_in'
                available = clip.end_us-clip.start_us > self.lane.fade_duration(clip, other)
            action.setEnabled(bool(selected and (active or available)))

    def toggle_fade(self, mode: str) -> None:
        if self.locked or self.worker or not 0 <= self.row < len(self.clips):
            return
        clip = self.clips[self.row]
        active = self.lane.fade_active(clip, mode)
        other = 'fade_out' if mode == 'fade_in' else 'fade_in'
        available = clip.end_us-clip.start_us-self.lane.fade_duration(clip, other)
        if not active and available <= 0:
            return
        duration = 0 if active else min(500_000, max(1, (clip.end_us-clip.start_us)//4),
                                       available)
        update: dict[str, int | None] = {mode+'_us': duration,
                  ('envelope_start_us' if mode == 'fade_in' else 'envelope_end_us'):
                      clip.start_us if mode == 'fade_in' else clip.end_us}
        if duration == 0 and not getattr(clip, other+'_us'):
            update['envelope_start_us'] = None
            update['envelope_end_us'] = None
        changed = MusicClip.model_validate(clip.model_dump() | update)
        self.commit([*self.clips[:self.row], changed, *self.clips[self.row+1:]], self.row)
        self.update_fade_actions()

    def drag_edit(self, row: int, mode: str, delta: int) -> None:
        if self.locked or self.worker:
            return
        self.select(row)
        if mode in ('fade_in', 'fade_out'):
            if not self.lane.fade_active(self.clips[row], mode):
                return
            delta = self.lane.bounded_delta(row, mode, delta)
            c = self.clips[row]
            value = self.lane.fade_duration(c, mode)+(delta if mode == 'fade_in' else -delta)
            c = c.model_copy(update={mode+'_us': value,
                'envelope_start_us': c.start_us if mode == 'fade_in' else c.envelope_start_us,
                'envelope_end_us': c.end_us if mode == 'fade_out' else c.envelope_end_us})
            self.commit([*self.clips[:row], c, *self.clips[row+1:]], row)
            return
        if mode == 'right':
            try:
                path = self.clips[row].path
                if path not in self.lane.source_lengths:
                    self.lane.source_lengths[path] = round(audio_duration(path, None)*1e6)
            except (OSError, ValueError, RuntimeError) as exc:
                self.show_error(str(exc))
                return
        delta = self.lane.bounded_delta(row, mode, delta)
        if mode == 'move':
            self.at.setValue(self.at.value()+delta/1e6)
        elif mode == 'left':
            self.at.setValue(self.at.value()+delta/1e6)
            self.start.setValue(self.start.value()+delta/1e6)
        else:
            self.end.setValue(self.end.value()+delta/1e6)
        self.change()

    def remove(self) -> None:
        row = self.row
        if 0 <= row < len(self.clips):
            self.commit(self.clips[:row]+self.clips[row+1:])

    def split(self) -> None:
        if self.worker or self.locked:
            return
        at = round(round(Fraction(self.lane.value()*1000)/self.frame_us)*self.frame_us)
        for row, clip in enumerate(self.clips):
            end = clip.at_us+clip.end_us-clip.start_us
            if clip.at_us < at < end:
                if min(at-clip.at_us, end-at)+1 < self.frame_us:
                    return
                source = clip.start_us+at-clip.at_us
                clip = clip.model_copy(update={
                    'envelope_start_us': (clip.envelope_start_us
                        if clip.envelope_start_us is not None else clip.start_us),
                    'envelope_end_us': (clip.envelope_end_us
                        if clip.envelope_end_us is not None else clip.end_us)})
                left = clip.model_copy(update={'end_us': source})
                right = clip.model_copy(update={'start_us': source, 'at_us': at})
                self.commit([*self.clips[:row], left, right, *self.clips[row+1:]], row+1)
                return

    def undo(self) -> None:
        self.replay(False)

    def redo(self) -> None:
        self.replay(True)

    def replay(self, redo: bool) -> None:
        if self.worker or self.locked:
            return
        stack = self.redo_stack if redo else self.undo_stack
        if stack:
            try:
                previous = stack[-1]
                save_draft(self.root, self.video, previous)
                stack.pop()
                (self.undo_stack if redo else self.redo_stack).append(self.clips)
                self.clips = previous
                self.player.pause()
                self.audition_ready = False
                self.monitor()
                self.refresh(self.row)
                self.play_state(self.player.playbackState())
                self.message.setText(tr('Правки сохранены. Нажмите «Слушать», чтобы проверить.'))
            except (OSError, ValueError) as exc:
                self.show_error(str(exc))

    def work(self, operation: Callable[[list[MusicClip], Event], Path], applied: bool) -> None:
        if self.worker:
            return
        position = self.lane.playhead_us//1000
        if position >= self.duration//1000-50:
            position = 0
        self.wave_manager.shutdown()
        self.seek_timer.stop()
        self.pending_seek_ms = None
        self.player.stop()
        self.player.setSource(QUrl())
        clips = list(self.clips)
        self.worker = MusicJob(lambda cancel: operation(clips, cancel))
        for widget in self.edit_widgets:
            widget.setEnabled(False)
        def ready(path: str) -> None:
            if applied:
                self.saved_path = path
                self.version_saved.emit(path)
                self.message.setText(tr('Копия с ручной музыкой сохранена.'))
            else:
                self.message.setText(tr('Просмотр музыки с видео.'))
            self.pending_seek_ms = position
            self.player.setSource(QUrl.fromLocalFile(path))
            self.audition_ready = True
            self.monitor()
            self.player.play()
        self.worker.result.connect(ready)
        self.worker.error.connect(self.show_error)
        self.worker.finished.connect(self.finished_work)
        self.message.setText(tr('Готовим музыку…'))
        self.worker.start()

    def finished_work(self) -> None:
        if self.worker:
            self.worker.deleteLater()
            self.worker = None
        for widget in self.edit_widgets:
            widget.setEnabled(True)
        self.refresh(self.row)

    def preview(self) -> None:
        if self.locked:
            self.toggle_play()
            return
        def operation(clips: list[MusicClip], cancel: Event) -> Path:
            music = assemble_music(self.root, clips, self.duration, cancel)
            self.serial += 1
            path = Path(self.scratch.name)/f'preview-{self.serial}.mp4'
            mux_music(self.video, music, path, self.duration, cancel)
            return path
        self.work(operation, False)

    def apply(self) -> None:
        if self.locked:
            return
        self.work(lambda clips, cancel: apply_music_edit(
            self.root, self.video, clips, cancel), True)

    def reject(self) -> None:
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.worker:
            self.worker.cancel.set()
            event.ignore()
            QTimer.singleShot(100, self.close)
            return
        self.wave_manager.shutdown()
        self.seek_timer.stop()
        self.pending_seek_ms = None
        self.player.stop()
        self.player.setSource(QUrl())
        self.scratch.cleanup()
        self.closed.emit(self.saved_path)
        event.accept()
