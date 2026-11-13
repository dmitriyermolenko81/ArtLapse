"""Try scene alternatives against the selected final video's fixed scene slots."""

import logging
import math
from dataclasses import replace
from pathlib import Path
from threading import Event

from PySide6.QtCore import QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QIcon, QImage, QKeySequence, QPixmap, QResizeEvent, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from sculptcut.audio import DEFAULT_MUSIC_FADE_OUT_SECONDS
from sculptcut.combined_edits import CombinedEdit
from sculptcut.domain import Roi
from sculptcut.episode_context_preview import ContextClip, ContextPreparer, PreviewScene
from sculptcut.episode_images import EpisodeImages, ImageKey, image_key
from sculptcut.episode_playback import EpisodePlayback
from sculptcut.episode_roll import CHOICES
from sculptcut.episode_roll_context import RollContext, load_roll_context
from sculptcut.i18n import tr
from sculptcut.intro import body_start_frame
from sculptcut.media import Cancelled
from sculptcut.playback_volume import PlaybackVolume, VolumeControl
from sculptcut.proxy import proxy_path
from sculptcut.rendering import export_size
from sculptcut.time_format import format_time
from sculptcut.ui_icons import ui_icon
from sculptcut.ui_theme import apply_dialog_theme


class RollLoader(QThread):
    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, root: Path, video: Path, cuts: list[tuple[int, int]]) -> None:
        super().__init__()
        self.root, self.video, self.cuts = root, video, cuts
        self.cancel = Event()

    def run(self) -> None:
        try:
            context = load_roll_context(self.root, self.video, self.cuts, self.cancel)
            if not self.cancel.is_set():
                self.ready.emit(context)
        except Cancelled:
            pass
        except (OSError, ValueError, RuntimeError) as exc:
            logging.getLogger("artlapse").exception("Cannot prepare scene alternatives")
            if not self.cancel.is_set():
                self.failed.emit(str(exc))
        except Exception as exc:
            logging.getLogger("artlapse").exception("Unexpected scene-loading failure")
            if not self.cancel.is_set():
                self.failed.emit(f"{type(exc).__name__}: {exc}")


_loaders: set[RollLoader] = set()


class EpisodeRollDialog(QDialog):
    ready = Signal()
    def __init__(self, root: Path, video: Path, parent: QWidget, *, position: int = 0,
                 cuts: list[tuple[int, int]] | None = None,
                 volume_state: PlaybackVolume | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Замена эпизодов · таймлапс"))
        apply_dialog_theme(self)
        self.resize(1000, 800)
        self.setMinimumSize(760, 660)
        self.video = video
        self.closed = False
        self.loading = True
        self.initial_position = position
        self.requested_volume = volume_state
        self.loading_layout = QVBoxLayout(self)
        self.loading_box = QWidget()
        loading = QVBoxLayout(self.loading_box)
        self.loading_label = QLabel(tr("Загружаем эпизоды…"))
        self.loading_label.setWordWrap(True)
        loading.addStretch(1)
        loading.addWidget(self.loading_label)
        self.loading_bar = QProgressBar()
        self.loading_bar.setRange(0, 0)
        loading.addWidget(self.loading_bar)
        cancel = QPushButton(tr("Закрыть"))
        cancel.clicked.connect(self.reject)
        loading.addWidget(cancel)
        loading.addStretch(1)
        self.loading_layout.addWidget(self.loading_box)
        self.loader = RollLoader(root, video, list(cuts or []))
        _loaders.add(self.loader)
        self.loader.ready.connect(self.loaded)
        self.loader.failed.connect(self.load_failed)
        self.loader.finished.connect(lambda: _loaders.discard(self.loader))
        self.loader.finished.connect(self.loader.deleteLater)
        QTimer.singleShot(0, self.loader.start)

    def load_failed(self, message: str) -> None:
        if not self.closed:
            self.loading_bar.hide()
            self.loading_label.setText(tr(message))

    def loaded(self, context: RollContext) -> None:
        if self.closed:
            return
        self.context_rows: list[int] = []
        self.context_serial = 0
        self.context_ready = False
        self.context_clip: ContextClip | None = None
        self.context_preparer = ContextPreparer(self)
        self.context_preparer.ready.connect(self.context_prepared)
        self.context_preparer.failed.connect(self.context_failed)
        self.playback_row = 0
        self.context = context
        self.edit, self.roll = context.edit, context.roll
        self.video_spans = context.video_spans
        project = self.edit.project
        self.loading_box.hide()
        self.content = QWidget()
        self.loading_layout.addWidget(self.content)
        self.row = 0
        self.frame_image = QImage()
        self.picture_key: ImageKey | None = None
        self.preview_original = True
        self.preview_start = 0
        self.preview_end = 1
        self.images = EpisodeImages(self, limit=8, width=960)
        self.images.ready.connect(self.thumbnail)
        self.images.failed.connect(self.failure)
        self.option_images = EpisodeImages(self, limit=32, width=192)
        self.option_images.ready.connect(self.option_thumbnail)
        self.playback = EpisodePlayback(self)
        self.playback.limit_frames = True
        self.playback.audio.setMuted(True)
        self.playback.image_ready.connect(self.video_frame)
        self.playback.position_changed.connect(self.position_changed)
        self.playback.playing_changed.connect(self.playing_changed)
        self.playback.failed.connect(self.failure)
        self.playback.finished.connect(self.scene_finished)
        self.music = QMediaPlayer(self)
        self.music_audio = QAudioOutput(self)
        self.music.setAudioOutput(self.music_audio)
        soundtrack = self.edit.project.settings.music_path
        if soundtrack is not None:
            self.music.setSource(QUrl.fromLocalFile(str(soundtrack.resolve())))
        if self.edit.project.settings.music_loop:
            self.music.setLoops(QMediaPlayer.Loops.Infinite)
        self.music_should_play = False
        self.desired_music_ms = 0
        self.force_music_sync = False
        self.music_timer = QTimer(self)
        self.music_timer.setInterval(40)
        self.music_timer.timeout.connect(self.sync_music)
        self.music_timer.start()
        self.volume_state = self.requested_volume or PlaybackVolume(parent=self)
        self.volume_state.bind(self.music_audio)

        layout = QVBoxLayout(self.content)
        layout.setContentsMargins(0, 0, 0, 0)
        hint = QLabel(tr("Выберите сцену в монтаже и листайте варианты между соседями. "
                         "Время склеек и музыка сохраняются."))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.picture = QLabel()
        self.picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.picture.setMinimumHeight(180)
        self.picture.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.picture.setStyleSheet("background:#0c1118; border-radius:8px;")
        layout.addWidget(self.picture, 1)
        self.scrub = QSlider(Qt.Orientation.Horizontal)
        self.scrub.setRange(0, 1000)
        self.scrub.sliderMoved.connect(self.seek)
        layout.addWidget(self.scrub)
        playback_row = QHBoxLayout()
        playback_row.addWidget(VolumeControl(self.volume_state))
        self.info = QLabel()
        self.info.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        playback_row.addWidget(self.info, 1)
        self.play = QPushButton(tr("Просмотр"))
        self.play.setIcon(ui_icon("play"))
        self.play.clicked.connect(self.toggle_play)
        playback_row.addWidget(self.play)
        self.context_play = QPushButton(tr("С соседями"))
        self.context_play.setIcon(ui_icon("play"))
        self.context_play.setToolTip(tr(
            "Предыдущая сцена → выбранный вариант → следующая, с музыкой"))
        self.context_play.clicked.connect(self.toggle_context)
        playback_row.addWidget(self.context_play)
        layout.addLayout(playback_row)
        self.context_status = QLabel()
        self.context_status.setWordWrap(True)
        self.context_status.hide()
        layout.addWidget(self.context_status)
        self.strip = QListWidget()
        self.strip.setFlow(QListView.Flow.LeftToRight)
        self.strip.setWrapping(False)
        self.strip.setFixedHeight(84)
        self.strip.setStyleSheet("QListWidget::item { padding:3px 9px; margin:2px; }")
        self.strip.setHorizontalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.strip.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.strip.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.strip.setAccessibleName(tr("Эпизоды готового ролика"))
        offset = body_start_frame(project.settings, self.roll.base.output_fps,
                                  self.roll.base.actual_frame_count)
        offset -= sum(b-a for a, b in self.edit.leading)
        self.offset = offset
        for i, slot in enumerate(self.roll.slots):
            a = (offset + slot.output_start) / self.roll.base.output_fps
            b = a + slot.original.output_frame_count / self.roll.base.output_fps
            item = QListWidgetItem(f"{i+1}\n{format_time(a, 1)}–{format_time(b, 1)}")
            item.setSizeHint(QSize(max(130, self.strip.fontMetrics().horizontalAdvance(
                f"{format_time(a, 1)}–{format_time(b, 1)}") + 24), 56))
            self.strip.addItem(item)
        self.strip.currentRowChanged.connect(self.select_slot)
        layout.addWidget(self.strip)
        self.bounds = QLabel()
        self.bounds.setWordWrap(True)
        self.bounds.setToolTip(tr("До {0} лучших замен по подтверждённым изменениям. "
            "Близкие по времени окна не дублируются.").format(CHOICES.maximum_alternatives))
        layout.addWidget(self.bounds)
        self.options_strip = QListWidget()
        self.options_strip.setAccessibleName(tr("Доступные варианты выбранной сцены"))
        self.options_strip.setViewMode(QListView.ViewMode.IconMode)
        self.options_strip.setFlow(QListView.Flow.LeftToRight)
        self.options_strip.setWrapping(False)
        self.options_strip.setIconSize(QSize(96, 54))
        self.options_strip.setGridSize(QSize(190, 120))
        self.options_strip.setFixedHeight(140)
        self.options_strip.setHorizontalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.options_strip.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.options_strip.itemClicked.connect(self.choose_option)
        self.options_strip.horizontalScrollBar().valueChanged.connect(self.request_options)
        layout.addWidget(self.options_strip)
        alternatives = QHBoxLayout()
        self.earlier = QPushButton(tr("Раньше"))
        self.later = QPushButton(tr("Позже"))
        self.earlier.setIcon(ui_icon("previous"))
        self.later.setIcon(ui_icon("next"))
        self.earlier.setAccessibleName(tr("Предыдущий вариант эпизода"))
        self.later.setAccessibleName(tr("Следующий вариант эпизода"))
        self.earlier.clicked.connect(lambda: self.step(-1))
        self.later.clicked.connect(lambda: self.step(1))
        alternatives.addWidget(self.earlier)
        self.candidate = QLabel()
        self.candidate.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.candidate.setWordWrap(True)
        alternatives.addWidget(self.candidate, 1)
        alternatives.addWidget(self.later)
        layout.addLayout(alternatives)
        footer = QHBoxLayout()
        self.undo = QPushButton(tr("Отменить"))
        self.redo = QPushButton(tr("Повторить"))
        self.undo.clicked.connect(lambda: self.replay(False))
        self.redo.clicked.connect(lambda: self.replay(True))
        footer.addWidget(self.undo)
        footer.addWidget(self.redo)
        footer.addStretch(1)
        cancel = QPushButton(tr("Закрыть"))
        cancel.clicked.connect(self.reject)
        footer.addWidget(cancel)
        self.export = QPushButton(tr("Обновить предпросмотр"))
        self.export.setProperty("actionRole", "primary")
        self.export.clicked.connect(self.accept)
        footer.addWidget(self.export)
        layout.addLayout(footer)
        self.message = QLabel(tr("Правки обновят черновик. Финальный экспорт — в главном окне."))
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        for key, action in (("Space", self.toggle_play), ("Ctrl+Z", lambda: self.replay(False)),
                            ("Ctrl+Y", lambda: self.replay(True))):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(action)
        row = next((i for i, (a, b) in enumerate(self.video_spans) if self.initial_position < b),
                   len(self.roll.slots)-1)
        self.strip.setCurrentRow(row)
        self.loading = False
        self.ready.emit()

    def select_slot(self, row: int) -> None:
        if 0 <= row < len(self.roll.slots):
            self.row = row
            self.strip.blockSignals(True)
            self.strip.setCurrentRow(row)
            self.strip.blockSignals(False)
            self.strip.scrollToItem(self.strip.item(row))
            self.refresh()

    def step(self, direction: int) -> None:
        if self.roll.step(self.row, direction):
            self.refresh()

    def replay(self, redo: bool) -> None:
        self.roll.undo(redo=redo)
        self.refresh()

    def refresh(self) -> None:
        self.cancel_context()
        slot = self.roll.slots[self.row]
        d = self.roll.choices[self.row]
        self.preview_original = d == slot.original
        options = self.roll.options(self.row)
        self.visible_options = options
        index = options.index(d)
        self.earlier.setEnabled(index > 0)
        self.later.setEnabled(index + 1 < len(options))
        self.update_options(index)
        self.undo.setEnabled(bool(self.roll.undo_stack))
        self.redo.setEnabled(bool(self.roll.redo_stack))
        self.export.setEnabled(self.roll.changed)
        fps = self.roll.base.output_fps
        duration = d.output_frame_count / fps
        speed = (d.source_end_us-d.source_start_us) / 1e6 / duration
        self.info.setText(tr("Место {0} · {1} с · {2}×").format(
            self.row+1, f"{duration:.2f}", f"{speed:.2f}"))
        if d.action == "HOLD":
            self.candidate.setText(tr("Финальный стоп-кадр"))
        else:
            self.candidate.setText(tr("Вариант {0} из {1} · исходник {2}–{3}").format(
                index+1, len(options), format_time(d.source_start_us / 1e6),
                format_time(d.source_end_us / 1e6)))
        self.prepare_scene(self.row)
        for i, choice in enumerate(self.roll.choices):
            self.strip.item(i).setToolTip(tr("Заменён") if choice != self.roll.slots[i].original
                                         else tr("Исходный вариант"))

    def prepare_scene(self, row: int) -> None:
        """View a draft slot without moving the editing selection or altering its plan."""
        self.playback.pause()
        self.playback_row = row
        self.frame_image = QImage()
        self.picture.clear()
        self.picture.setText(tr("Загрузка кадра…"))
        self.music_should_play = False
        d = self.roll.choices[row]
        self.preview_original = d == self.roll.slots[row].original
        duration = d.output_frame_count / self.roll.base.output_fps
        speed = (d.source_end_us-d.source_start_us) / 1e6 / duration
        if self.preview_original:
            self.preview_start, self.preview_end = self.video_spans[row]
            path, rate = self.video, 1.0
        else:
            source = next(s for s in self.roll.project.sources if s.id == d.source_id)
            proxy = proxy_path(self.loader.root, self.roll.project, source)
            path, rate = proxy if proxy.is_file() else source.path, speed
            self.preview_start, self.preview_end = d.source_start_us//1000, d.source_end_us//1000
        self.playback.player.setPlaybackRate(rate)
        self.playback.select(path, self.preview_start, self.preview_end, self.preview_start)
        self.position_changed(self.preview_start, force_music=True)
        self.picture_key = image_key(path, self.preview_start*1000)
        self.images.request([self.picture_key])
        self.scrub.setValue(0)
        if self.context_rows:
            sequence = " → ".join(str(i+1) for i in self.context_rows)
            self.context_status.setText(tr("Просмотр сцен {0} · сейчас {1} · замена в сцене {2}")
                                        .format(sequence, row+1, self.row+1))

    def cancel_context(self) -> None:
        self.context_serial += 1
        self.context_ready = False
        self.context_clip = None
        self.context_preparer.cancel()
        self.play.setEnabled(True)
        self.context_rows = []
        self.context_play.setText(tr("С соседями"))
        self.context_status.hide()

    def toggle_context(self) -> None:
        if self.context_rows:
            self.refresh()
            return
        self.context_serial += 1
        self.context_rows = list(range(max(0, self.row-1),
                                      min(len(self.roll.slots), self.row+2)))
        self.playback.pause()
        self.context_ready = False
        self.play.setEnabled(False)
        self.context_play.setText(tr("Остановить"))
        self.context_status.setText(tr("Подготавливаем плавный просмотр… Можно отменить."))
        self.context_status.show()
        scenes = []
        for row in self.context_rows:
            d = self.roll.choices[row]
            if d == self.roll.slots[row].original:
                a, b = self.video_spans[row]
                scenes.append(PreviewScene(row, self.video, a, b, d.output_frame_count))
            else:
                source = next(s for s in self.roll.project.sources if s.id == d.source_id)
                proxy = proxy_path(self.loader.root, self.roll.project, source)
                path = proxy if proxy.is_file() else source.path
                scenes.append(PreviewScene(row, path, d.source_start_us//1000,
                                           d.source_end_us//1000, d.output_frame_count, d))
        width, height = export_size(self.roll.project, self.roll.base)
        scale = min(1.0, 960/max(width, height))
        size = max(2, int(width*scale)//2*2), max(2, int(height*scale)//2*2)
        self.context_preparer.request(tuple(scenes), self.roll.base.output_fps, size)

    def context_prepared(self, clip: ContextClip) -> None:
        if self.closed or not self.context_rows:
            return
        self.context_clip = clip
        self.context_ready = True
        self.preview_original = True  # Crop and speed are already applied to the short preview.
        self.preview_start, self.preview_end = 0, clip.spans[-1][1]
        self.play.setEnabled(True)
        self.playback.player.setPlaybackRate(1.0)
        self.playback.select(clip.path, 0, self.preview_end, 0)
        self.position_changed(0, force_music=True)
        self.playback.play()

    def context_failed(self, message: str) -> None:
        if not self.closed and self.context_rows:
            self.refresh()
            self.message.setText(tr("Не удалось подготовить просмотр: {0}").format(message))

    def scene_finished(self) -> None:
        if self.context_rows and self.context_ready:
            serial = self.context_serial
            QTimer.singleShot(0, lambda: self.advance_context(serial, self.playback_row))

    def advance_context(self, serial: int, row: int) -> None:
        if not self.closed and serial == self.context_serial and self.context_ready:
            self.refresh()

    def thumbnail(self, key: ImageKey, image: QImage) -> None:
        if (key == self.picture_key and not self.closed and not self.context_ready
                and (not self.playback.want_play or self.playback.awaiting_seek)):
            self.show_frame(image)

    def video_frame(self, image: QImage, timestamp: int) -> None:
        if self.playback.want_play and self.preview_start <= timestamp < self.preview_end:
            if self.context_ready:
                self.position_changed(timestamp)
            self.show_frame(image)

    def update_options(self, selected: int) -> None:
        sources = {s.id:s for s in self.roll.project.sources}

        def neighbour(row: int) -> str:
            d = self.roll.choices[row]
            return tr("сцена {0} · {1} · {2}–{3}").format(row+1, sources[d.source_id].path.name,
                format_time(d.source_start_us/1e6, 1), format_time(d.source_end_us/1e6, 1))

        left = neighbour(self.row-1) if self.row else tr("Начало проекта")
        right = (neighbour(self.row+1) if self.row+1 < len(self.roll.slots)
                 else tr("Конец проекта"))
        original = self.roll.slots[self.row].original
        baseline = tr("В видео: {0} · {1}–{2}").format(sources[original.source_id].path.name,
            format_time(original.source_start_us/1e6, 1),
            format_time(original.source_end_us/1e6, 1))
        self.bounds.setText(tr("Границы выбора: {0} → {1}\nЛучшие варианты: {2}").format(
            left, right, len(self.visible_options)) + "\n" + baseline)
        self.option_keys: dict[ImageKey, list[int]] = {}
        self.options_strip.clear()
        for i, d in enumerate(self.visible_options):
            path = proxy_path(self.loader.root, self.roll.project, sources[d.source_id])
            path = path if path.is_file() else sources[d.source_id].path
            key = image_key(path, d.source_start_us)
            self.option_keys.setdefault(key, []).append(i)
            status = tr("В видео") if d == original else (
                tr("Выбрана замена") if i == selected else tr("Вариант замены"))
            item = QListWidgetItem(f"{i+1}. {format_time(d.source_start_us/1e6, 1)}–"
                                   f"{format_time(d.source_end_us/1e6, 1)}\n{status}")
            if d == original:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            item.setData(Qt.ItemDataRole.UserRole, i)
            item.setToolTip(sources[d.source_id].path.name)
            self.options_strip.addItem(item)
        self.options_strip.setCurrentRow(selected)
        self.options_strip.scrollToItem(self.options_strip.item(selected))
        QTimer.singleShot(0, self.request_options)

    def request_options(self) -> None:
        if self.closed or not hasattr(self, "option_keys"):
            return
        viewport = self.options_strip.viewport().rect()
        self.option_images.request([key for key, rows in self.option_keys.items() if any(
            self.options_strip.visualItemRect(self.options_strip.item(i)).intersects(viewport)
            for i in rows)])

    def option_thumbnail(self, key: ImageKey, image: QImage) -> None:
        if not self.closed:
            for row in self.option_keys.get(key, []):
                self.options_strip.item(row).setIcon(QIcon(QPixmap.fromImage(image)))

    def choose_option(self, item: QListWidgetItem) -> None:
        self.roll.select(self.row, self.visible_options[item.data(Qt.ItemDataRole.UserRole)])
        self.refresh()

    def show_frame(self, image: QImage) -> None:
        crop = self.roll.choices[self.playback_row].crop
        if not self.preview_original and isinstance(crop, Roi):
            image = image.copy(round(crop.x * image.width()), round(crop.y * image.height()),
                               round(crop.width * image.width()),
                               round(crop.height * image.height()))
        self.frame_image = image
        self.fit_picture()

    def fit_picture(self) -> None:
        if not self.frame_image.isNull():
            self.picture.setPixmap(QPixmap.fromImage(self.frame_image).scaled(
                self.picture.size(), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if hasattr(self, "picture"):
            self.fit_picture()

    def toggle_play(self) -> None:
        if self.context_rows and not self.context_ready:
            return
        if self.playback.want_play:
            self.playback.pause()
        else:
            self.playback.play()

    def playing_changed(self, playing: bool) -> None:
        self.music_should_play = playing
        if not playing:
            self.music_audio.setVolume(0)
            QTimer.singleShot(0, self.sync_music)
        self.play.setText(tr("Пауза") if playing else tr("Просмотр"))
        self.play.setIcon(ui_icon("pause" if playing else "play"))
        if playing:
            position = (self.playback.seek_target if self.playback.awaiting_seek
                        else self.playback.player.position())
            self.position_changed(position, force_music=True)

    def position_changed(self, position: int, *, force_music: bool = False) -> None:
        if not hasattr(self, "scrub") or self.context_rows and not self.context_ready:
            return
        fraction = max(0.0, min(1.0, (position-self.preview_start)
                               / max(1, self.preview_end-self.preview_start)))
        if not self.scrub.isSliderDown():
            self.scrub.setValue(round(fraction*1000))
        music_fraction = fraction
        if self.context_ready and self.context_clip:
            start, end, row = next((span for span in self.context_clip.spans if position < span[1]),
                                   self.context_clip.spans[-1])
            self.playback_row = row
            music_fraction = max(0.0, min(1.0, (position-start)/max(1, end-start)))
            sequence = " → ".join(str(i+1) for i in self.context_rows)
            self.context_status.setText(tr("Просмотр сцен {0} · сейчас {1} · замена в сцене {2}")
                                        .format(sequence, row+1, self.row+1))
        a, b = self.video_spans[self.playback_row]
        self.desired_music_ms = round(a + music_fraction*(b-a))
        self.force_music_sync |= force_music

    def silence_music(self) -> None:
        self.music_audio.setVolume(0)
        if self.music.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.music.pause()

    def sync_music(self) -> None:
        """Audio pause never waits for readiness; seeks stay outside video callbacks."""
        if self.closed or not self.music_should_play or not self.playback.is_advancing():
            self.silence_music()
            return
        if self.music.mediaStatus() not in (
            QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia,
            QMediaPlayer.MediaStatus.EndOfMedia,
        ):
            self.silence_music()
            return
        target = self.desired_music_ms
        settings = self.edit.project.settings
        song_ms = self.music.duration()
        if settings.music_loop and song_ms:
            target %= song_ms
        audible = settings.music_loop or target < song_ms
        duration = self.context.duration_ms / 1000
        seconds = self.desired_music_ms / 1000
        fade_in = min(settings.music_fade_seconds, duration/2)
        fade_out = min(DEFAULT_MUSIC_FADE_OUT_SECONDS, duration)
        envelope = min(1.0, seconds/fade_in) if fade_in else 1.0
        if fade_out:
            envelope *= math.sin(math.pi/2*max(0.0, min(1.0,
                (duration-seconds)/fade_out)))**2
        self.music_audio.setVolume(self.volume_state.level/100 * envelope if audible else 0)
        if self.force_music_sync or abs(self.music.position()-target) > 120:
            self.force_music_sync = False
            self.music.setPosition(target)
        # Native seek may dispatch a newer pause/end while synchronizing audio.
        if self.closed or not self.music_should_play or not self.playback.is_advancing():
            self.silence_music()
            return
        playing = self.music.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        if audible and not playing:
            self.music.play()
        elif not audible and playing:
            self.music.pause()

    def seek(self, value: int) -> None:
        if self.context_rows:
            self.refresh()
        position = self.preview_start+(self.preview_end-self.preview_start)*value//1000
        self.playback.seek(position)
        if not self.playback.want_play:
            path = Path(self.playback.player.source().toLocalFile())
            self.picture_key = image_key(path, min(position, self.preview_end-1)*1000)
            self.images.request([self.picture_key])

    def failure(self, message: str) -> None:
        if hasattr(self, "context_play"):
            self.playback.pause()
            self.cancel_context()
        if hasattr(self, "message"):
            self.message.setText(message)

    def export_edit(self) -> CombinedEdit:
        plan = self.roll.plan()
        return replace(self.edit, plan=plan, base_plan=plan)

    def shutdown(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.loader.cancel.set()
        if self.loading:
            return
        self.cancel_context()
        self.images.shutdown()
        self.option_images.shutdown()
        self.playback.shutdown()
        self.context_preparer.shutdown()
        self.music_timer.stop()
        self.music.stop()
        self.music.setSource(QUrl())

    def done(self, result: int) -> None:
        self.shutdown()
        super().done(result)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.shutdown()
        super().closeEvent(event)
