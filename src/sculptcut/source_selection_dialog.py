"""Experimental two-clock editor: montage scenes above exact source selections."""

import json
from pathlib import Path

from PySide6.QtCore import (
    QCoreApplication,
    QDateTime,
    QSignalBlocker,
    QSize,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import QCloseEvent, QIcon, QImage, QPixmap, QResizeEvent, QValidator
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sculptcut.combined_edits import prepare_combined_edit
from sculptcut.domain import Project
from sculptcut.episode_context_preview import (
    ContextClip,
    ContextPreparer,
    PreviewAudio,
    PreviewScene,
    shorts_scenes,
)
from sculptcut.episode_images import EpisodeImages, ImageKey, image_key
from sculptcut.episode_playback import EpisodePlayback
from sculptcut.hover_timeline_keys import HoverTimelineKeys
from sculptcut.i18n import tr
from sculptcut.inline_music import InlineMusic
from sculptcut.intro import body_start_frame, output_frame_count
from sculptcut.montage_playback import (
    MontagePlayback,
    bookend_scenes,
    rendered_segments,
    reused_scene,
)
from sculptcut.montage_timeline import MontageTimeline
from sculptcut.music_edit import LOCKED_MUSIC_REASON
from sculptcut.output_cuts import retained_ranges, saved_soundtrack
from sculptcut.planning import RenderPlan
from sculptcut.playback_volume import PlaybackVolume, VolumeControl
from sculptcut.project import load_project
from sculptcut.proxy import proxy_path
from sculptcut.review_timeline import ReviewTimeline
from sculptcut.scrub_frames import ScrubFrames
from sculptcut.selection_duration import selection_frames, timeline_frames, timeline_start
from sculptcut.selection_history import (
    ManualEdit,
    edit_origin,
    manual_edits,
    reference_before_manual,
    scoped_edits,
)
from sculptcut.selection_timeline import SelectionTimeline
from sculptcut.shorts_refill import cuts_on_plan_clock
from sculptcut.simple_timeline import EditMarker, SimpleTimeline
from sculptcut.source_selection import SourceSelection
from sculptcut.time_format import format_time
from sculptcut.timeline_appearance import LanePiece
from sculptcut.timewarp import output_before, source_time
from sculptcut.ui_icons import ui_icon
from sculptcut.ui_theme import apply_dialog_theme
from sculptcut.variants import read_variant


class SourceTime(QDoubleSpinBox):
    def __init__(self) -> None:
        super().__init__()
        self.setRange(0, 1e9)
        self.setDecimals(2)
        self.setSingleStep(0.1)
        self.setKeyboardTracking(False)
        self.setMinimumWidth(120)

    def textFromValue(self, value: float) -> str:
        return format_time(value, 2)

    def valueFromText(self, text: str) -> float:
        result = 0.0
        for part in text.replace(",", ".").split(":"):
            result = result * 60 + float(part)
        return result

    def validate(self, text: str, position: int) -> tuple[QValidator.State, str, int]:
        try:
            self.valueFromText(text)
            return QValidator.State.Acceptable, text, position
        except ValueError:
            return QValidator.State.Intermediate, text, position


_loaders: set["SelectionLoader"] = set()


class SelectionLoader(QThread):
    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, root: Path, video: Path, cuts: list[tuple[int, int]],
                 fixed_duration: bool = False) -> None:
        super().__init__()
        self.root, self.video, self.cuts = root, video, cuts
        self.fixed_duration = fixed_duration

    def run(self) -> None:
        try:
            edit = prepare_combined_edit(
                self.root, self.video, self.cuts, apply_current=False, exact_variant=True
            )
            # A version owns its montage; newer project rules belong to later
            # versions. Only its validated per-video draft restores pending edits.
            if not self.isInterruptionRequested():
                session = SourceSelection(edit, fixed_duration=self.fixed_duration)
                neutral = read_variant(self.video).get('episode_base_plan')
                if neutral:
                    session.reference = reference_before_manual(
                        session.project, RenderPlan.model_validate(neutral))
                from sculptcut.selection_draft import restore_draft

                try:
                    restore_draft(self.root, self.video, session)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    session.restore_note = str(exc)
                if (self.fixed_duration and manual_edits(
                        session.project, session.reference, session.plan)
                        and selection_frames(session.project, session.plan,
                            session.original.leading, session.original.trailing)
                        != round(session.project.settings.target_seconds*session.plan.output_fps)):
                    session.fit_duration()
                self.ready.emit(session)
        except (OSError, ValueError, RuntimeError) as exc:
            if not self.isInterruptionRequested():
                self.failed.emit(str(exc))


class SourceSelectionDialog(QDialog):
    music_changed = Signal()
    music_message = Signal(str)
    ready = Signal()
    preview_image = Signal(QImage)
    apply_requested = Signal()
    cursor_changed = Signal(int)
    plan_changed = Signal()

    def __init__(
        self,
        root: Path,
        video: Path,
        parent: QWidget,
        *,
        cuts: list[tuple[int, int]] | None = None,
        position: int = 0,
        volume_state: PlaybackVolume | None = None,
        compact: bool = False,
        modern: bool = False,
        restore_navigation: bool = True,
    ) -> None:
        super().__init__(parent)
        self.compact = compact
        self.modern = compact and modern
        self.restore_navigation = restore_navigation
        apply_dialog_theme(self)
        self.setWindowTitle(tr("Подрезка ролика"))
        self.resize(1080, 780)
        self.setMinimumSize(820, 660)
        self.root, self.video, self.initial_position = root, video, position
        self.loading, self.closed, self.context_ready = True, False, False
        self.music_track: InlineMusic | None = None
        self.hover_keys: HoverTimelineKeys | None = None
        self.active_index = -1
        self.context_serial = 0
        self.frame_image = QImage()
        self.layout_box = QVBoxLayout(self)
        self.message = QLabel(tr("Загружаем монтаж и исходники…"))
        self.message.setWordWrap(True)
        self.layout_box.addWidget(self.message)
        self.body = QWidget()
        self.body.setObjectName("selectionBody")
        self.layout_box.addWidget(self.body, 1)
        self.close_button = QPushButton(tr("Закрыть"))
        self.close_button.clicked.connect(self.reject)
        self.layout_box.addWidget(self.close_button)
        self.volume_state = volume_state or PlaybackVolume(parent=self)
        self.loader = SelectionLoader(root, video, list(cuts or []), compact)
        _loaders.add(self.loader)
        self.loader.ready.connect(self.loaded)
        self.loader.failed.connect(self.message.setText)
        self.loader.finished.connect(lambda: _loaders.discard(self.loader))
        self.loader.finished.connect(self.loader.deleteLater)
        app = QCoreApplication.instance()
        if app:
            app.aboutToQuit.connect(self.wait_loader)
        QTimer.singleShot(0, self.loader.start)

    def wait_loader(self) -> None:
        if self.loading and self.loader in _loaders:
            self.loader.requestInterruption()
            self.loader.wait(3000)

    def loaded(self, session: SourceSelection) -> None:
        if self.closed:
            return
        self.session = session
        saved = read_variant(self.video)
        saved_settings = Project.model_validate(saved["project"]).settings
        self.saved_plan = RenderPlan.model_validate(saved["plan"])
        soundtrack = saved_soundtrack(self.video)
        # refill_render_context resolves a music library into the preserved track.
        # The library path itself does not change the already rendered video.
        saved_settings = saved_settings.model_copy(update={
            "music_path": soundtrack[0] if soundtrack else None, "music_folder": None})
        self.saved_settings_compatible = session.project.settings == saved_settings
        # Only cuts already baked into this MP4 affect its pixel clock. Pending
        # edits change the current plan, not the saved video's frame positions.
        baked = cuts_on_plan_clock(self.video, Project.model_validate(saved["project"]),
                                   self.saved_plan, [])
        fps = self.saved_plan.output_fps
        total = output_frame_count(saved_settings, fps, self.saved_plan.actual_frame_count)
        frame_cuts = [(max(0, min(total, a*fps//1_000_000)),
                       max(0, min(total, (b*fps+999_999)//1_000_000))) for a, b in baked]
        self.saved_frame_ranges = retained_ranges(total, [(a, b) for a, b in frame_cuts if a < b])
        self.saved_segments_compatible = (
            not session.original.cuts and not session.original.leading
            and not session.original.trailing
            and self.saved_settings_compatible)
        self.saved_video_compatible = (self.saved_segments_compatible
                                      and session.base.decisions == self.saved_plan.decisions)
        self.saved_segments = rendered_segments(session.project, self.saved_plan)
        self.saved_scene_cache: dict[int, PreviewScene | None] = {}
        self.saved_scrub = False
        if self.restore_navigation and session.restored_cursor is not None:
            self.initial_position = session.restored_cursor
        current_project = load_project(self.root)
        self.root_overrides = current_project.overrides
        self.root_splits = current_project.source_splits
        self.loading = False
        self.playback = EpisodePlayback(self)
        self.playback.limit_frames = True
        self.playback.audio.setMuted(True)
        self.playback.image_ready.connect(self.playback_image)
        self.playback.position_changed.connect(self.update_cursor)
        self.scrub_timer = QTimer(self)
        self.scrub_timer.setSingleShot(True)
        self.scrub_timer.setInterval(50)
        self.scrub_timer.timeout.connect(self.flush_scrub)
        self.navigation_timer = QTimer(self)
        self.navigation_timer.setSingleShot(True)
        self.navigation_timer.setInterval(16)
        self.navigation_timer.timeout.connect(self.navigate)
        self.pending_navigation: tuple[str, int, str, int] | None = None
        self.resume_navigation = False
        self.manual_choice: ManualEdit | None = None
        self.manual_ranges: list[ManualEdit] = []
        self.show_inherited_edits = False
        self.scrub_target = 0
        self.playback.playing_changed.connect(self.play_state)
        self.playback.failed.connect(self.fail)
        self.playback.finished.connect(self.finished_preview)
        self.volume_state.changed.connect(self.refresh_audio)
        self.images = EpisodeImages(self, width=960, limit=6)
        self.images.ready.connect(self.thumbnail)
        self.images.failed.connect(self.fail)
        self.scrub_frames = ScrubFrames(self)
        self.scrub_frames.ready.connect(self.scrub_frame_ready)
        self.scrub_frames.failed.connect(self.fail)
        self.cards = EpisodeImages(self, width=160, limit=24)
        self.cards.ready.connect(self.card_ready)
        self.card_keys: dict[ImageKey, list[int]] = {}
        self.cards_timer = QTimer(self)
        self.cards_timer.setSingleShot(True)
        self.cards_timer.setInterval(60)
        self.cards_timer.timeout.connect(self.request_cards)
        self.preparer = ContextPreparer(self)
        self.preparer.ready.connect(self.prepared)
        self.preparer.failed.connect(self.fail)
        self.sequence = MontagePlayback(self.playback, self)
        self.sequence.clock_changed.connect(self.sequence_position)
        self.sequence.failed.connect(self.fail)
        self.sequence.busy_changed.connect(self.sequence_busy)
        self.picture_key: ImageKey | None = None
        self.primary_action = "keep"
        self.speed_changed = False
        layout = QVBoxLayout(self.body)
        layout.setContentsMargins(0, 0, 0, 0)
        self.summary = QLabel()
        layout.addWidget(self.summary)
        self.montage_title = QLabel(tr("Выберите фрагмент ролика"))
        layout.addWidget(self.montage_title)
        self.montage = QListWidget()
        self.montage.setViewMode(QListView.ViewMode.IconMode)
        self.montage.setMovement(QListView.Movement.Static)
        self.montage.setGridSize(QSize(190, 90))
        self.montage.setFlow(QListView.Flow.LeftToRight)
        self.montage.setWrapping(False)
        self.montage.setFixedHeight(110)
        self.montage.setIconSize(QSize(96, 54))
        self.montage.horizontalScrollBar().valueChanged.connect(lambda: self.cards_timer.start())
        self.montage.setHorizontalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.montage.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.montage.currentItemChanged.connect(lambda item, old: self.choose_scene(item))
        self.montage.itemClicked.connect(self.reselect_scene)
        layout.addWidget(self.montage)
        navigation = QHBoxLayout()
        self.previous_scene = QPushButton(tr("Предыдущий эпизод"))
        self.next_scene = QPushButton(tr("Следующий эпизод"))
        self.previous_scene.clicked.connect(lambda: self.step_scene(-1))
        self.next_scene.clicked.connect(lambda: self.step_scene(1))
        navigation.addWidget(self.previous_scene)
        self.montage_seek = ReviewTimeline() if self.modern else (
            SimpleTimeline() if self.compact else MontageTimeline())
        if isinstance(self.montage_seek, SimpleTimeline):
            self.montage_seek.edge_preview.connect(self.request_edge_preview)
            self.montage_seek.scrub_finished.connect(self.finish_navigation)
            self.montage_seek.restore_edit.connect(self.restore_marked)
            self.montage_seek.revise_edit.connect(self.edit_marked)
            self.montage_seek.edge_commit.connect(self.commit_edge)
            self.montage_seek.movie_commit.connect(self.trim_movie)
            self.montage_seek.remove_scene.connect(lambda: self.change("drop"))
            self.montage_seek.split_scene.connect(self.split_scene)
            self.montage_seek.play_requested.connect(self.play_montage)
            self.montage_seek.undo_requested.connect(lambda: self.history(False))
            self.montage_seek.redo_requested.connect(lambda: self.history(True))
        if not self.compact:
            self.montage_seek.setToolTip(tr("Позиция в монтаже — перетащите курсор"))
        self.montage_seek.sliderMoved.connect(self.request_montage_seek)
        navigation.addWidget(self.montage_seek, 1)
        navigation.addWidget(self.next_scene)
        layout.addLayout(navigation)
        self.picture = QLabel()
        self.picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.picture.setMinimumHeight(170)
        self.picture.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.picture.setStyleSheet("background:#0c1118; border-radius:6px")
        layout.addWidget(self.picture, 1)
        playback_row = QHBoxLayout()
        self.play = QPushButton(tr("Просмотреть выделенное"))
        self.play.setMinimumWidth(200)
        self.play.clicked.connect(self.toggle_play)
        playback_row.addWidget(self.play)
        self.selection_label = QLabel()
        self.selection_label.setWordWrap(True)
        self.selection_label.setMinimumWidth(0)
        self.selection_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        playback_row.addWidget(self.selection_label, 1)
        self.volume_control = VolumeControl(self.volume_state)
        playback_row.addWidget(self.volume_control)
        layout.addLayout(playback_row)
        source_row = QHBoxLayout()
        self.source_label = QLabel(tr("Исходная запись"))
        source_row.addWidget(self.source_label)
        self.sources = QComboBox()
        for source in session.project.sources:
            self.sources.addItem(source.path.name, source.id)
        self.sources.currentIndexChanged.connect(self.choose_source)
        source_row.addWidget(self.sources, 1)
        self.timeline: SelectionTimeline = SelectionTimeline()
        zoom = QToolButton()
        zoom.setText(tr("Масштаб"))
        self.zoom_button = zoom
        zoom.setMinimumWidth(112)
        zoom.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(zoom)
        for text, action in (
            (tr("Ближе"), lambda: self.timeline.zoom(0.5)),
            (tr("Дальше"), lambda: self.timeline.zoom(2)),
            (tr("Весь исходник"), lambda: self.timeline.set_view(0, self.timeline.duration)),
        ):
            menu.addAction(text).triggered.connect(action)
        zoom.setMenu(menu)
        source_row.addWidget(zoom)
        layout.addLayout(source_row)
        self.range_hint = QLabel(tr("Выберите границы двумя жёлтыми ручками."))
        self.range_hint.setWordWrap(True)
        layout.addWidget(self.range_hint)
        self.timeline.selected.connect(self.select_range)
        self.timeline.scrubbed.connect(self.scrub_source)
        layout.addWidget(self.timeline)
        actions = QHBoxLayout()
        self.edit_scope = QToolButton()
        self.edit_scope.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.edit_scope.setFixedHeight(28)
        self.edit_scope.setStyleSheet('QToolButton {padding:3px 8px; font-size:11px; '
            'color:#b8c8dd; background:transparent; border:1px solid transparent; '
            'border-radius:4px;}'
            'QToolButton:hover {background:#273545; border-color:#405570;}'
            'QToolButton::menu-indicator {image:none;}')
        self.scope_menu = QMenu(self.edit_scope)
        self.scope_current = self.scope_menu.addAction(tr('Эта версия'))
        self.scope_all = self.scope_menu.addAction(tr('Все версии'))
        for scope_action, all_edits in ((self.scope_current, False), (self.scope_all, True)):
            scope_action.setCheckable(True)
            scope_action.triggered.connect(
                lambda checked=False, value=all_edits: self.set_edit_scope(value))
        self.edit_scope.setMenu(self.scope_menu)
        actions.addWidget(self.edit_scope)
        self.keep = QPushButton(tr("Добавить в ролик"))
        self.keep.setProperty("actionRole", "primary")
        self.drop = QPushButton(tr("Убрать из ролика"))
        self.keep.clicked.connect(lambda: self.change(self.primary_action))
        self.drop.clicked.connect(lambda: self.change("drop"))
        actions.addWidget(self.keep)
        actions.addWidget(self.drop)
        actions.addStretch(1)
        self.undo = QPushButton(tr("Отменить правку"))
        self.undo.clicked.connect(lambda: self.history(False))
        actions.addWidget(self.undo)
        self.more = QPushButton(tr("Дополнительно"))
        self.more.setCheckable(True)
        actions.addWidget(self.more)
        layout.addLayout(actions)
        self.details = QWidget()
        self.details.hide()
        self.more.toggled.connect(self.details.setVisible)
        extra = QVBoxLayout(self.details)
        extra.setContentsMargins(0, 0, 0, 0)
        bounds = QGridLayout()
        self.start, self.end = SourceTime(), SourceTime()
        for i, (name, field) in enumerate(((tr("Начало"), self.start), (tr("Конец"), self.end))):
            bounds.addWidget(QLabel(name), i if self.modern else 0, 0 if self.modern else i*2)
            bounds.addWidget(field, i if self.modern else 0, 1 if self.modern else i*2+1)
            field.editingFinished.connect(self.typed_range)
        bounds.addWidget(QLabel(tr("Скорость в монтаже")),
                         2 if self.modern else 0, 0 if self.modern else 4)
        self.speed = QDoubleSpinBox()
        self.speed.setRange(1, 128)
        self.speed.setValue(2)
        self.speed.setSuffix("×")
        self.speed.valueChanged.connect(self.speed_edited)
        bounds.addWidget(self.speed, 2 if self.modern else 0, 1 if self.modern else 5)
        extra.addLayout(bounds)
        tools = QVBoxLayout() if self.modern else QHBoxLayout()
        self.context = QPushButton(tr("Посмотреть с соседними фрагментами"))
        self.context.clicked.connect(self.preview_context)
        self.fit = QPushButton(tr("Подогнать под прежнюю длительность"))
        self.fit.clicked.connect(self.fit_duration)
        self.redo = QPushButton(tr("Повторить"))
        self.redo.clicked.connect(lambda: self.history(True))
        for button in (self.context, self.fit, self.redo):
            tools.addWidget(button)
        extra.addLayout(tools)
        if self.modern:
            actions.removeWidget(self.keep)
            extra.addWidget(self.keep)
        layout.addWidget(self.details)
        footer = QHBoxLayout()
        self.apply = QPushButton(tr("Сохранить изменения"))
        self.apply.clicked.connect(
            lambda: self.apply_requested.emit() if self.compact else self.accept())
        footer.addStretch(1)
        self.layout_box.removeWidget(self.close_button)
        self.close_button.setText(tr("Отмена"))
        footer.addWidget(self.close_button)
        footer.addWidget(self.apply)
        self.layout_box.addLayout(footer)
        self.manual_tools = QWidget()
        manual = QHBoxLayout(self.manual_tools)
        manual.setContentsMargins(0, 0, 0, 0)
        self.manual_caption = QLabel()
        manual.addWidget(self.manual_caption, 1)
        self.restore_part = QPushButton(tr('Вернуть участок'))
        self.restore_part.clicked.connect(self.restore_open_edit)
        self.save_part = QPushButton(tr('Сохранить границы'))
        self.save_part.clicked.connect(self.save_open_edit)
        self.close_part = QPushButton()
        self.close_part.setIcon(ui_icon('close'))
        self.close_part.setFixedWidth(28)
        self.close_part.setToolTip(tr('Вернуться к монтажу'))
        self.close_part.clicked.connect(self.close_manual_edit)
        for button in (self.restore_part, self.save_part, self.close_part):
            button.setFixedHeight(28)
            button.setStyleSheet('QPushButton {padding:3px 8px; font-size:12px;}')
            manual.addWidget(button)
        self.layout_box.addWidget(self.manual_tools)
        self.manual_tools.hide()
        self.message.setText(
            tr(
                "Клик по дорожке показывает кадр. Начало и конец меняются только "
                "за жёлтые ручки; во время движения виден кадр у границы."
            )
        )
        self.refresh()
        offset = timeline_start(session.project, session.plan, session.original.leading)
        clock = offset / session.plan.output_fps
        for row in range(self.montage.count()):
            item = self.montage.item(row)
            d = session.plan.decisions[item.data(Qt.ItemDataRole.UserRole)]
            clock += d.output_frame_count / session.plan.output_fps
            if self.initial_position / 1000 < clock:
                self.montage.setCurrentRow(row)
                break
        if self.compact:
            self.seek_montage(self.initial_position, follow=False)
        elif self.restore_navigation and session.restored_cursor is not None:
            self.seek_montage(session.restored_cursor)
        if (self.restore_navigation and session.restored_view is not None
                and isinstance(self.montage_seek, SimpleTimeline)):
            self.montage_seek.set_view(*session.restored_view)
        if self.compact:
            for widget in (self.summary, self.montage_title, self.montage, self.picture,
                           self.play, self.selection_label, self.volume_control,
                           self.sources, self.source_label, self.zoom_button, self.range_hint):
                widget.hide()
            self.message.hide()
            self.cards_timer.stop()
            self.layout_box.setContentsMargins(0, 0, 0, 0)
            self.layout_box.setSpacing(2)
            layout.setSpacing(2)
            self.previous_scene.setText("◀")
            self.next_scene.setText("▶")
            self.previous_scene.setToolTip(tr("Предыдущий эпизод"))
            self.next_scene.setToolTip(tr("Следующий эпизод"))
            self.previous_scene.setFixedWidth(32)
            self.next_scene.setFixedWidth(32)
            self.montage_seek.setFixedHeight(34)
            self.timeline.show_caption = False
            self.timeline.setFixedHeight(72)
            self.undo.setText("↶")
            self.more.setText("⋯")
            self.more.setToolTip(tr("Дополнительно"))
            self.undo.setToolTip(tr("Отменить правку"))
            self.close_button.setText("×")
            for button in (self.undo, self.more, self.close_button):
                button.setFixedWidth(32)
            self.drop.setText(tr("Убрать"))
            self.close_button.setToolTip(tr("Отменить изменения"))
            self.apply.setText(tr("Применить"))
            footer.removeWidget(self.close_button)
            footer.removeWidget(self.apply)
            actions.addWidget(self.close_button)
            actions.addWidget(self.apply)
            for extra_widget in (self.timeline, self.keep, self.drop, self.more,
                           self.close_button, self.previous_scene, self.next_scene):
                extra_widget.hide()
            self.undo.setText("")
            self.undo.setIcon(ui_icon("undo"))
            self.undo.setAccessibleName(tr("Отменить правку"))
            self.undo.setToolTip(tr("Отменить правку (Ctrl+Z)"))
            self.undo.setFixedSize(28, 28)
            self.undo.setStyleSheet("QPushButton {padding:0;}")
            self.apply.setText(tr("Применить правки"))
            self.apply.setToolTip(tr("Создать обновлённый ролик с текущими правками. "
                                     "Финальный экспорт выполняется отдельно."))
            self.apply.setFixedHeight(28)
            self.apply.setStyleSheet("QPushButton {padding:3px 10px; font-size:12px;}")
            self.montage_seek.setFixedHeight(72)
            self.setStyleSheet(self.styleSheet() + "\nQDialog {background:#181e26;}"
                              "QWidget#selectionBody {background:transparent;}")
        if self.modern:
            self.montage_seek.setFixedHeight(104)
            self.apply.setObjectName('primary')
            self.apply.setStyleSheet('')
            self.apply.setFixedHeight(34)
            self.more.setCheckable(False)
            self.more.setText('•••')
            self.more.setFixedSize(38, 30)
            self.more.setToolTip(tr('Действия с фрагментом'))
            self.more.show()
            self.keep.show()
            self.fragment_menu = QMenu(self.more)
            self.fragment_menu.addAction(tr('Точные параметры'), self.toggle_details)
            self.fragment_menu.addAction(tr('Разделить здесь (Q)'), self.split_scene)
            self.fragment_menu.addAction(
                tr('Убрать из ролика (Delete)'), lambda: self.change('drop'))
            self.fragment_menu.addSeparator()
            self.fragment_redo = self.fragment_menu.addAction(
                tr('Повторить правку (Ctrl+Y)'), lambda: self.history(True))
            self.more.clicked.connect(lambda: self.fragment_menu.popup(
                self.more.mapToGlobal(self.more.rect().bottomLeft())))
            for button in (self.keep, self.drop, self.undo, self.more, self.apply,
                           self.close_button, self.context, self.fit, self.redo, self.play,
                           self.previous_scene, self.next_scene, self.restore_part,
                           self.save_part, self.close_part, self.volume_control.mute_button):
                button.setAutoDefault(False)
        if not self.isWindow():
            self.picture.setMinimumHeight(100)
            self.montage.setFixedHeight(90)
        if session.restore_note:
            self.message.setText(session.restore_note)
            self.message.show()
        if self.compact and isinstance(self.montage_seek, SimpleTimeline):
            self.montage_seek.follow(self.montage_seek.value(), self.montage.currentRow())
        if self.modern:
            assert isinstance(self.montage_seek, ReviewTimeline)
            self.montage_seek.compact_lane = True
            self.montage_seek.left_padding = 56
            self.montage_seek.lane_label = tr('Видео')
            self.montage_seek.marker_y = 56
            self.montage_seek.setFixedHeight(62)
            try:
                self.music_track = InlineMusic(self)
                music = self.music_track
                button = music.options_button
                button.hide()
                self.more.hide()
                button.setFixedSize(28, 28)
                button.setStyleSheet('QPushButton {padding:0;}')
                button.setText('♫')
                button.setToolTip(tr('Музыкальная дорожка'))
                button.setAccessibleName(tr('Музыкальная дорожка'))
                button.pressed.connect(music.lane.setFocus)
                if music.locked:
                    button.setText('🔒')
                    button.setToolTip(tr(LOCKED_MUSIC_REASON))
                    button.setEnabled(False)
                layout.insertWidget(layout.indexOf(self.picture), self.music_track)
                self.music_track.show()
                self.hover_keys = HoverTimelineKeys(self)
                self.apply.setEnabled(self.session.changed or self.music_track.changed)
            except (OSError, ValueError, RuntimeError) as exc:
                self.message.setText(tr(str(exc)))
                self.message.show()
        self.ready.emit()
        if self.compact:
            self.scrub_frames.prepare_video(self.video)

    def toggle_details(self) -> None:
        self.details.setVisible(not self.details.isVisible())
        if self.details.isVisible():
            self.start.setFocus()

    def refresh(self) -> None:
        self.navigation_timer.stop()
        self.pending_navigation = None
        self.resume_navigation = False
        self.sequence.stop()
        self.saved_scene_cache.clear()
        self.montage.blockSignals(True)
        self.montage.clear()
        self.card_keys = {}
        plan = self.session.plan
        if isinstance(self.montage_seek, SimpleTimeline):
            self.montage_seek.frame_ms = 1000 / float(plan.output_fps)
            self.montage_seek.split_positions = []
        split_points = self.session.split_points()
        self.montage_seek.blocks = []
        if isinstance(self.montage_seek, ReviewTimeline):
            self.montage_seek.pieces = []
        clock = (timeline_start(self.session.project, plan, self.session.original.leading)
                 / plan.output_fps)
        for i, d in enumerate(plan.decisions):
            if not d.output_frame_count:
                continue
            duration = d.output_frame_count / plan.output_fps
            if isinstance(self.montage_seek, SimpleTimeline):
                for timestamp, output in ((d.source_start_us, clock),
                                          (d.source_end_us, clock+duration)):
                    if (d.source_id, timestamp) in split_points:
                        self.montage_seek.split_positions.append(round(float(output)*1000))
            self.montage_seek.blocks.append((self.montage.count(), round(float(clock) * 1000),
                                             round(float(clock + duration) * 1000)))
            if isinstance(self.montage_seek, ReviewTimeline):
                source = next(s for s in self.session.project.sources if s.id == d.source_id)
                self.montage_seek.pieces.append(LanePiece(self.montage.count(),
                    float(clock)*1000, float(clock+duration)*1000,
                    f'{self.montage.count()+1} · {source.path.stem}',
                    f'{format_time(d.source_start_us/1e6, 1)} – '
                    f'{format_time(d.source_end_us/1e6, 1)}', str(self.montage.count()+1),
                    tr('{0} с').format(f'{float(duration):.1f}')))
            item = QListWidgetItem(
                f"{self.montage.count() + 1} · "
                f"{format_time(clock, 1)}–{format_time(clock + duration, 1)}"
            )
            item.setData(Qt.ItemDataRole.UserRole, i)
            item.setSizeHint(QSize(190, 80))
            self.montage.addItem(item)
            clock += duration
        self.montage.blockSignals(False)
        if isinstance(self.montage_seek, SimpleTimeline):
            self.montage_seek.split_positions = sorted(set(self.montage_seek.split_positions))
        total = (
            timeline_frames(self.session.project, plan,
                            self.session.original.leading, self.session.original.trailing)
            / plan.output_fps
        )
        self.summary.setText(
            tr("Ролик: {0} · фрагментов: {1}").format(format_time(total, 2), self.montage.count())
        )
        self.montage_seek.setRange(0, max(0, round(float(total) * 1000) - 1))
        self.undo.setEnabled(bool(self.session.undo_stack))
        self.redo.setEnabled(bool(self.session.redo_stack))
        if self.modern and hasattr(self, 'fragment_redo'):
            self.fragment_redo.setEnabled(bool(self.session.redo_stack))
        self.apply.setEnabled(self.session.changed)
        self.active_index = -1
        self.montage.setCurrentRow(0)
        self.cards_timer.start()
        self.update_manual_marks()
        self.plan_changed.emit()

    def set_edit_scope(self, all_edits: bool) -> None:
        self.show_inherited_edits = all_edits
        self.update_manual_marks()

    def update_manual_marks(self) -> None:
        self.manual_ranges = scoped_edits(self.session.project, self.session.base,
            self.session.reference, self.session.plan, self.show_inherited_edits)
        current_count = len(scoped_edits(self.session.project, self.session.base,
                                        self.session.reference, self.session.plan))
        self.scope_current.setChecked(not self.show_inherited_edits)
        self.scope_all.setChecked(self.show_inherited_edits)
        caption = tr('Все правки: {0}') if self.show_inherited_edits else tr('Правки версии: {0}')
        self.edit_scope.setText(caption.format(len(self.manual_ranges))+' ▾')
        self.scope_current.setText(tr('Эта версия ({0})').format(current_count))
        self.edit_scope.setToolTip(tr('Серое — вырезано, зелёное — добавлено.\n'
                                     'Старые правки доступны в «Все версии».'))
        if not isinstance(self.montage_seek, SimpleTimeline):
            return
        plan = self.session.plan
        head = timeline_start(self.session.project, plan, self.session.original.leading)
        marks = []
        for key, edit in enumerate(self.manual_ranges):
            clock = head
            left = right = None
            removed_frames = 0
            for d in plan.decisions:
                if (d.source_id == edit.source_id and d.source_start_us < edit.end_us
                        and edit.start_us < d.source_end_us):
                    a, b = max(edit.start_us, d.source_start_us), min(edit.end_us, d.source_end_us)
                    start = clock+(output_before(d, a) if d.output_frame_count else 0)
                    end = clock+(output_before(d, b) if d.output_frame_count else 0)
                    left = start if left is None else min(left, start)
                    right = end if right is None else max(right, end)
                clock += d.output_frame_count
            reference = self.session.reference if edit.inherited else self.session.base
            for d in reference.decisions:
                if (d.source_id == edit.source_id and d.output_frame_count
                        and d.source_start_us < edit.end_us and edit.start_us < d.source_end_us):
                    removed_frames += output_before(d, min(d.source_end_us, edit.end_us))
                    removed_frames -= output_before(d, max(d.source_start_us, edit.start_us))
            if left is not None and right is not None:
                label = tr('Вырезано') if edit.kind == 'removed' else (
                    tr('Добавлено') if edit.kind == 'added' else tr('Изменена скорость'))
                label += f' · {format_time(edit.start_us/1e6, 1)}–{format_time(edit.end_us/1e6, 1)}'
                label += ' · '+(tr('Ранее') if edit.inherited else tr('Эта версия'))
                origin = edit_origin(plan, edit)
                stamp = QDateTime.fromString(origin.get('created_at', ''), Qt.DateFormat.ISODate)
                detail = label
                if stamp.isValid():
                    detail += '\n'+stamp.toLocalTime().toString('dd.MM HH:mm')
                if origin.get('version'):
                    detail += ' · '+origin['version']
                marks.append(EditMarker(key, round(left*1000/plan.output_fps),
                    round(right*1000/plan.output_fps), edit.kind == 'removed', label,
                    edit.kind, edit.inherited, detail, round(removed_frames*1000/plan.output_fps)))
        self.montage_seek.edits = marks
        self.montage_seek.update()

    def restore_marked(self, key: int) -> None:
        try:
            self.session.restore_manual(self.manual_ranges[key])
            self.refresh()
        except (ValueError, IndexError) as exc:
            self.fail(str(exc))

    def edit_marked(self, key: int) -> None:
        edit = self.manual_ranges[key]
        self.manual_choice = edit
        self.manual_cursor = self.montage_seek.value()
        self.sources.blockSignals(True)
        self.sources.setCurrentIndex(self.sources.findData(edit.source_id))
        self.sources.blockSignals(False)
        self.update_bands()
        self.timeline.edits = [e for e in scoped_edits(self.session.project, self.session.base,
            self.session.reference, self.session.plan, True) if e.source_id == edit.source_id]
        self.active_index = -1
        self.select_range(edit.start_us, edit.end_us)
        self.manual_caption.setText(tr('Вырезанный участок') if edit.action == 'DROP'
                                    else tr('Ручная правка'))
        self.restore_part.setText(tr('Вернуть участок' if edit.kind == 'removed' else
            'Отменить добавление' if edit.kind == 'added' else 'Вернуть скорость'))
        self.timeline.show()
        self.montage_seek.hide()
        self.manual_tools.show()
        self.edit_scope.hide()
        self.apply.hide()
        self.undo.hide()

    def close_manual_edit(self) -> None:
        self.manual_choice = None
        self.playback.pause()
        self.manual_tools.hide()
        self.edit_scope.show()
        self.timeline.hide()
        self.montage_seek.show()
        self.apply.show()
        self.undo.show()
        self.refresh()
        self.seek_montage(self.manual_cursor)

    def restore_open_edit(self) -> None:
        if self.manual_choice is not None:
            try:
                self.session.restore_manual(self.manual_choice)
                self.close_manual_edit()
            except ValueError as exc:
                self.fail(str(exc))

    def save_open_edit(self) -> None:
        if self.manual_choice is not None:
            try:
                self.session.restore_manual(self.manual_choice,
                                            self.timeline.start, self.timeline.end)
                self.close_manual_edit()
            except ValueError as exc:
                self.fail(str(exc))

    def request_cards(self) -> None:
        if self.closed or self.compact:
            return
        self.card_keys = {}
        sources = {s.id: s for s in self.session.project.sources}
        rows = [self.montage.currentRow()] + [
            i for i in range(self.montage.count()) if i != self.montage.currentRow()
        ]
        for row in rows:
            if row < 0:
                continue
            item = self.montage.item(row)
            if not self.montage.visualItemRect(item).intersects(self.montage.viewport().rect()):
                continue
            d = self.session.plan.decisions[item.data(Qt.ItemDataRole.UserRole)]
            source = sources[d.source_id]
            proxy = proxy_path(self.root, self.session.project, source)
            key = image_key(proxy if proxy.is_file() else source.path, d.source_start_us)
            self.card_keys.setdefault(key, []).append(row)
        self.cards.request(list(self.card_keys))

    def card_ready(self, key: ImageKey, image: QImage) -> None:
        if not self.closed:
            for row in self.card_keys.get(key, []):
                self.montage.item(row).setIcon(QIcon(QPixmap.fromImage(image)))

    def reselect_scene(self, item: QListWidgetItem) -> None:
        d = self.session.plan.decisions[item.data(Qt.ItemDataRole.UserRole)]
        if (
            self.context_ready
            or self.speed_changed
            or self.timeline.start != d.source_start_us
            or self.timeline.end != d.source_end_us
        ):
            self.choose_scene(item)

    def choose_scene(self, item: QListWidgetItem | None, *, show_frame: bool = True) -> None:
        if item is None:
            return
        if isinstance(self.montage_seek, SimpleTimeline):
            self.montage_seek.selected_row = self.montage.row(item)
            self.montage_seek.update()
        self.active_index = item.data(Qt.ItemDataRole.UserRole)
        d = self.session.plan.decisions[self.active_index]
        self.sources.blockSignals(True)
        self.sources.setCurrentIndex(self.sources.findData(d.source_id))
        self.sources.blockSignals(False)
        self.update_bands()
        self.timeline.focus(d.source_start_us, d.source_end_us)
        self.speed.blockSignals(True)
        self.speed.setValue(
            max(
                1,
                (d.source_end_us - d.source_start_us)
                * self.session.plan.output_fps
                / 1e6
                / d.output_frame_count,
            )
        )
        self.speed.blockSignals(False)
        self.speed_changed = False
        self.select_range(d.source_start_us, d.source_end_us, show_frame=show_frame)
        if show_frame and isinstance(self.montage_seek, SimpleTimeline):
            self.montage_seek.follow(self.montage_seek.value(), self.montage.currentRow())

    def update_bands(self) -> None:
        sid = self.sources.currentData()
        source = next(s for s in self.session.project.sources if s.id == sid)
        self.timeline.duration = source.duration_us
        self.timeline.bands = [d for d in self.session.plan.decisions if d.source_id == sid]

    def choose_source(self) -> None:
        self.update_bands()
        self.active_index = -1
        self.timeline.focus(0, min(3_000_000, self.timeline.duration))
        self.select_range(self.timeline.start, self.timeline.end)

    def typed_range(self) -> None:
        self.select_range(round(self.start.value() * 1e6), round(self.end.value() * 1e6))

    def select_range(self, start: int, end: int, *, show_frame: bool = True) -> None:
        try:
            self.session.source_bounds(self.sources.currentData(), start, end)
        except ValueError as exc:
            self.message.setText(str(exc))
            return
        self.stop_context()
        self.sequence.stop()
        self.scrub_timer.stop()
        self.start.setValue(start / 1e6)
        self.end.setValue(end / 1e6)
        self.timeline.start, self.timeline.end = start, end
        if start < self.timeline.view_start or end > self.timeline.view_end:
            self.timeline.focus(start, end)
        self.timeline.update()
        if show_frame and not self.seek_saved_frame(start):
            self.saved_scrub = False
            source = next(s for s in self.session.project.sources
                          if s.id == self.sources.currentData())
            # Analysis proxies contain only 5 fps; use originals for new footage.
            path = source.path
            if self.compact:
                self.scrub_frames.request(image_key(path, start))
            else:
                self.playback.player.setPlaybackRate(1)
                self.playback.select(path, start // 1000, end // 1000, start // 1000)
                self.picture_key = image_key(path, start)
                self.images.request([self.picture_key])
        self.update_selection_actions(start, end)
        self.selection_label.setText(
            tr("Выделено в исходнике: {0}–{1}").format(
                format_time(start / 1e6, 2), format_time(end / 1e6, 2)
            )
        )

    def step_scene(self, delta: int) -> None:
        row = max(0, min(self.montage.count() - 1, self.montage.currentRow() + delta))
        self.montage.setCurrentRow(row)
        self.montage.scrollToItem(self.montage.currentItem())

    def edge_bounds(self, row: int, edge: str, ms: int) -> tuple[int, int, int, float]:
        index = self.montage.item(row).data(Qt.ItemDataRole.UserRole)
        d = self.session.plan.decisions[index]
        if d.action in ("HOLD", "MONTAGE"):
            raise ValueError(tr("Финальный кадр защищён. Выберите рабочий фрагмент."))
        _, left, right = self.montage_seek.blocks[row]
        span = d.source_end_us-d.source_start_us
        source = next(s for s in self.session.project.sources if s.id == d.source_id)
        others = [x for i, x in enumerate(self.session.plan.decisions)
                  if i != index and x.source_id == d.source_id and x.output_frame_count]
        low = max([0] + [x.source_end_us for x in others if x.source_end_us <= d.source_start_us])
        high = min([source.duration_us] + [x.source_start_us for x in others
                                         if x.source_start_us >= d.source_end_us])
        a, b = d.source_start_us, d.source_end_us
        value = d.source_start_us + round((ms-left)/max(1, right-left)*span)
        if edge == "start":
            a = max(low, min(value, b-50_000))
        else:
            b = min(high, max(value, a+50_000))
        speed = float(span*self.session.plan.output_fps/1e6/d.output_frame_count)
        return index, a, b, max(1, speed)

    def preview_edge(self, row: int, edge: str, ms: int) -> None:
        try:
            if self.sequence.enabled:
                self.choose_scene(self.montage.item(row))
            self.montage.setCurrentRow(row)
            _, a, b, _ = self.edge_bounds(row, edge, ms)
            self.scrub_source(a if edge == "start" else b-1)
        except ValueError as exc:
            self.fail(str(exc))

    def request_montage_seek(self, ms: int) -> None:
        self.resume_navigation = self.resume_navigation or self.sequence.desired
        self.pending_navigation = ('seek', 0, '', ms)
        if not self.navigation_timer.isActive():
            self.navigation_timer.start()

    def request_edge_preview(self, row: int, edge: str, ms: int) -> None:
        self.resume_navigation = False
        self.pending_navigation = ('edge', row, edge, ms)
        if not self.navigation_timer.isActive():
            self.navigation_timer.start()

    def navigate(self) -> None:
        if isinstance(self.montage_seek, SimpleTimeline) and self.montage_seek.pointer_down:
            self.flush_navigation()
        else:
            self.finish_navigation()

    def finish_navigation(self) -> None:
        pending = self.pending_navigation
        resume, self.resume_navigation = self.resume_navigation, False
        if (resume and pending is not None and pending[0] == 'seek'
                and self.sequence.seek_running(pending[3])):
            self.navigation_timer.stop()
            self.pending_navigation = None
            return
        self.flush_navigation()
        if resume and not self.closed:
            self.play_montage()

    def flush_navigation(self) -> None:
        self.navigation_timer.stop()
        pending, self.pending_navigation = self.pending_navigation, None
        if pending is None or self.closed:
            return
        kind, row, edge, ms = pending
        if kind == 'seek':
            self.seek_montage(ms, follow=False)
        else:
            self.preview_edge(row, edge, ms)

    def commit_edge(self, row: int, edge: str, ms: int) -> None:
        self.navigation_timer.stop()
        self.pending_navigation = None
        self.resume_navigation = False
        try:
            index, a, b, speed = self.edge_bounds(row, edge, ms)
            sid = self.session.plan.decisions[index].source_id
            self.session.resize(index, a, b, speed)
            self.refresh()
            self.refocus(sid, a)
            self.scrub_source(a if edge == "start" else b-1)
        except ValueError as exc:
            self.fail(str(exc))

    def split_scene(self) -> None:
        if self.loading or self.manual_choice is not None:
            return
        track = self.montage_seek
        view = (track.view_start, track.view_span) if isinstance(track, SimpleTimeline) else None
        self.flush_navigation()
        position = self.montage_seek.value()
        try:
            focus = self.session.split_at(position)
            if focus is None:
                return
            self.edit_focus = focus
            self.refresh()
            self.refocus(*focus)
            self.seek_montage(position, follow=False)
            if view is not None and isinstance(track, SimpleTimeline):
                track.set_view(*view)
        except ValueError as exc:
            self.fail(str(exc))

    def trim_movie(self, start: int, end: int) -> None:
        try:
            self.session.trim_movie(start, end)
            self.refresh()
        except ValueError as exc:
            self.fail(str(exc))

    def seek_saved_frame(self, us: int) -> bool:
        """Scrub an unchanged rendered scene without reopening hours of source video."""
        if not self.compact or not self.saved_settings_compatible or self.active_index < 0:
            return False
        d = self.session.plan.decisions[self.active_index]
        if not d.source_start_us <= us < d.source_end_us:
            return False
        if self.active_index not in self.saved_scene_cache:
            self.saved_scene_cache[self.active_index] = reused_scene(
                d, self.video, self.saved_plan, self.saved_segments, self.montage.currentRow(),
                retimed=True)
        scene = self.saved_scene_cache[self.active_index]
        if scene is None:
            return False
        frame = 0 if d.action == "HOLD" else output_before(d, us)
        frame = min(scene.frames-1, frame)
        original_count = round((scene.end_ms-scene.start_ms)*self.saved_plan.output_fps/1000)
        original_frame = round(scene.start_ms*self.saved_plan.output_fps/1000)+min(
            original_count-1, 0 if d.action == 'HOLD' else output_before(
                d.model_copy(update={'output_frame_count': original_count}), us))
        elapsed = 0
        saved_frame = None
        for a, b in self.saved_frame_ranges:
            if a <= original_frame < b:
                saved_frame = elapsed+original_frame-a
                break
            elapsed += b-a
        if saved_frame is None:
            return False  # This source frame was removed from the saved MP4.
        ms = round(saved_frame*1000/self.saved_plan.output_fps)
        self.saved_scrub = True
        self.picture_key = None
        self.images.request([])
        self.scrub_timer.stop()
        self.scrub_frames.request(image_key(self.video, ms*1000), forward=True)
        self.timeline.playhead_us = us
        self.timeline.update()
        row = self.montage.currentRow()
        if 0 <= row < len(self.montage_seek.blocks):
            self.set_montage_cursor(self.montage_seek.blocks[row][1]
                                   + round(frame*1000/self.session.plan.output_fps))
        return True

    def scrub_source(self, us: int) -> None:
        if self.sequence.enabled:
            self.select_range(self.timeline.start, self.timeline.end)
        if self.context_ready:
            self.select_range(self.timeline.start, self.timeline.end)
        self.playback.pause()
        if self.seek_saved_frame(us):
            return
        self.saved_scrub = False
        source = next(s for s in self.session.project.sources
                      if s.id == self.sources.currentData())
        if self.compact:
            self.picture_key = None
            self.scrub_frames.request(image_key(source.path, us))
            self.update_cursor(us//1000)
            return
        if Path(self.playback.player.source().toLocalFile()) != source.path.resolve():
            self.playback.select(source.path, 0, source.duration_us//1000, us//1000)
        self.playback.limit_frames = False
        self.picture_key = None
        self.scrub_target = us // 1000
        if not self.scrub_timer.isActive():
            self.scrub_timer.start()

    def flush_scrub(self) -> None:
        if not self.closed:
            self.playback.seek(self.scrub_target)

    def update_cursor(self, ms: int) -> None:
        if hasattr(self, "sequence") and self.sequence.enabled:
            return
        if self.saved_scrub:
            return  # Saved-video PTS is a different clock from source timestamps.
        if self.loading or self.context_ready or not hasattr(self, "timeline"):
            return
        self.timeline.playhead_us = ms * 1000
        self.timeline.update()
        if not self.montage_seek.isSliderDown():
            for row, start, end in self.montage_seek.blocks:
                d = self.session.plan.decisions[
                    self.montage.item(row).data(Qt.ItemDataRole.UserRole)]
                if (d.source_id == self.sources.currentData()
                        and d.source_start_us <= ms * 1000 < d.source_end_us):
                    ratio = (ms * 1000 - d.source_start_us) / max(
                        1, d.source_end_us - d.source_start_us)
                    self.set_montage_cursor(start + round(ratio * (end - start)))
                    break

    def seek_montage(self, ms: int, *, follow: bool = True) -> None:
        self.sequence.stop()
        plan = self.session.plan
        if self.seek_bookend(ms):
            self.set_montage_cursor(ms)
            if follow and isinstance(self.montage_seek, SimpleTimeline):
                self.montage_seek.follow(ms)
            return
        clock = float(timeline_start(self.session.project, plan, self.session.original.leading)
                      / plan.output_fps * 1000)
        for row in range(self.montage.count()):
            item = self.montage.item(row)
            d = plan.decisions[item.data(Qt.ItemDataRole.UserRole)]
            duration = float(d.output_frame_count / plan.output_fps * 1000)
            if ms < clock + duration or row == self.montage.count() - 1:
                unchanged = (self.montage.currentRow() == row
                    and self.active_index == item.data(Qt.ItemDataRole.UserRole)
                    and self.timeline.start == d.source_start_us
                    and self.timeline.end == d.source_end_us
                    and not self.context_ready and not self.speed_changed)
                if not unchanged:
                    self.montage.blockSignals(True)
                    self.montage.setCurrentRow(row)
                    self.montage.blockSignals(False)
                    self.choose_scene(item, show_frame=False)
                ratio = max(0.0, min(0.999, (ms - clock) / max(1, duration)))
                # Use the same nonlinear speed-ramp clock as rendering.
                self.scrub_source(source_time(d, ratio))
                self.set_montage_cursor(ms)
                if follow and isinstance(self.montage_seek, SimpleTimeline):
                    self.montage_seek.follow(ms)
                return
            clock += duration

    def seek_bookend(self, ms: int) -> bool:
        if not self.compact or not self.saved_settings_compatible:
            return False
        plan, project = self.session.plan, self.session.project
        fps = plan.output_fps
        head = body_start_frame(project.settings, fps, plan.actual_frame_count)
        tail = output_frame_count(project.settings, fps, plan.actual_frame_count) \
            - head-plan.actual_frame_count
        start = timeline_start(project, plan, self.session.original.leading)
        body_end = start+plan.actual_frame_count
        if ms < round(start*1000/fps):
            saved, clock, count, key = 0, 0, head, 'selection_head'
            original = self.session.original.leading
        elif ms >= round(body_end*1000/fps) and tail:
            saved = body_start_frame(project.settings, fps, self.saved_plan.actual_frame_count) \
                + self.saved_plan.actual_frame_count
            clock, count, key = body_end, tail, 'selection_tail'
            original = self.session.original.trailing
        else:
            return False
        cuts = [tuple(p) for p in json.loads(str(plan.settings.get(key, json.dumps(original))))]
        scenes = bookend_scenes(self.video, clock, saved, count, cuts, self.saved_frame_ranges,
            fps, plan.settings.get('selection_duration_policy') == 'fixed')
        for a, b, scene in scenes:
            if a <= ms < b or ms == b == self.montage_seek.maximum():
                self.stop_context()
                self.playback.pause()
                self.saved_scrub = True
                self.picture_key = None
                self.images.request([])
                self.scrub_timer.stop()
                target = min(scene.end_ms-1, scene.start_ms+max(0, ms-a))
                self.scrub_frames.request(image_key(self.video, target*1000), forward=True)
                return True
        return False

    def play_montage(self, episode_only: bool = False) -> None:
        if self.manual_choice is not None:
            if self.playback.want_play:
                self.playback.pause()
            else:
                source = next(s for s in self.session.project.sources
                              if s.id == self.manual_choice.source_id)
                a, b = self.timeline.start//1000, self.timeline.end//1000
                cursor = max(a, min(self.timeline.playhead_us//1000, b-1))
                self.scrub_frames.wanted = None
                self.playback.limit_frames = True
                self.playback.select(source.path, a, b, cursor)
                self.playback.audio.setMuted(True)
                self.playback.play()
            return
        self.resume_navigation = False
        if self.music_track is not None and not self.music_track.prepare_playback(episode_only):
            return
        self.flush_navigation()
        if self.sequence.enabled and self.sequence.desired and not episode_only:
            self.sequence.toggle()
            return
        if self.sequence.enabled and self.sequence.episode_only == episode_only:
            self.sequence.toggle()
            return
        self.stop_context()
        self.scrub_frames.wanted = None
        self.scrub_timer.stop()
        cursor = self.montage_seek.value()
        if episode_only:
            row = max(0, self.montage.currentRow())
            cursor = self.montage_seek.blocks[row][1]
        self.sequence.start(self.session.project, self.session.plan, cursor, episode_only,
                            self.video, self.saved_plan.actual_frame_count,
                            direct=self.saved_video_compatible and not self.session.changed,
                            original=self.saved_plan if self.saved_settings_compatible else None,
                            saved_ranges=self.saved_frame_ranges,
                            reuse_stream=not self.session.changed,
                            leading=self.session.original.leading,
                            trailing=self.session.original.trailing)
        self.refresh_audio()

    def sequence_position(self, ms: int) -> None:
        self.set_montage_cursor(ms)
        row = next((row for row, a, b in self.montage_seek.blocks if a <= ms < b), None)
        if row is not None:
            self.montage.blockSignals(True)
            self.montage.setCurrentRow(row)
            self.montage.blockSignals(False)
            if isinstance(self.montage_seek, SimpleTimeline):
                self.montage_seek.selected_row = row
                self.montage_seek.update()
        if isinstance(self.montage_seek, SimpleTimeline):
            self.montage_seek.follow(ms, row)

    def set_montage_cursor(self, ms: int) -> None:
        # QAbstractSlider.setValue emits sliderMoved while the mouse is down.
        # Decoder/model updates are output, never another user seek request.
        with QSignalBlocker(self.montage_seek):
            self.montage_seek.setValue(ms)
        self.cursor_changed.emit(self.montage_seek.value())

    def sequence_busy(self, busy: bool) -> None:
        if busy:
            self.message.setText(tr("Готовим просмотр с курсора…"))
            self.message.show()
        elif self.compact:
            self.message.hide()

    def speed_edited(self) -> None:
        self.speed_changed = True
        self.update_selection_actions(
            round(self.start.value() * 1e6), round(self.end.value() * 1e6)
        )

    def update_selection_actions(self, start: int, end: int) -> None:
        sid = self.sources.currentData()
        hits = [
            (i, d)
            for i, d in enumerate(self.session.plan.decisions)
            if d.source_id == sid
            and d.output_frame_count
            and d.source_start_us < end
            and start < d.source_end_us
        ]
        protected = any(d.action in ("HOLD", "MONTAGE") for _, d in hits)
        self.primary_action = "keep"
        self.keep.setEnabled(not protected)
        self.drop.setEnabled(bool(hits) and not protected)
        if len(hits) == 1 and not protected:
            i, d = hits[0]
            self.active_index = i
            self.primary_action = "resize"
            identical = (
                start == d.source_start_us and end == d.source_end_us and not self.speed_changed
            )
            self.keep.setText(tr("Уже в ролике") if identical else tr("Оставить выделенную часть"))
            self.keep.setEnabled(not identical)
            self.range_hint.setText(
                tr(
                    "Это часть ролика. Сдвиньте ручки, чтобы убрать края "
                    "или захватить больше исходника."
                )
            )
        elif protected:
            self.keep.setText(tr("Финальный кадр"))
            self.range_hint.setText(tr("Финальный кадр защищён. Выберите рабочий фрагмент."))
        else:
            self.keep.setText(tr("Добавить в ролик"))
            self.range_hint.setText(
                tr("Серое не вошло в ролик. Выделите нужную часть и добавьте её.")
            )
            if not self.speed_changed:
                self.speed.blockSignals(True)
                self.speed.setValue(2)
                self.speed.blockSignals(False)

    def pending_selection(self) -> SourceSelection:
        preview = SourceSelection(self.session.export_edit())
        a, b = round(self.start.value() * 1e6), round(self.end.value() * 1e6)
        if self.primary_action == "resize":
            d = preview.plan.decisions[self.active_index]
            if a != d.source_start_us or b != d.source_end_us or self.speed_changed:
                preview.resize(self.active_index, a, b, self.speed.value())
        else:
            preview.select(self.sources.currentData(), a, b, self.speed.value())
        return preview

    def change(self, action: str) -> None:
        sid, a, b = (
            self.sources.currentData(),
            round(self.start.value() * 1e6),
            round(self.end.value() * 1e6),
        )
        try:
            if action == "resize":
                self.session.resize(self.active_index, a, b, self.speed.value())
            else:
                self.session.select(sid, a, b, None if action == "drop" else self.speed.value())
            self.refresh()
            self.edit_focus = sid, a
            self.refocus(sid, a)
            self.message.setText(
                tr("Правка готова. Сохраните изменения, когда закончите редактирование.")
            )
        except ValueError as exc:
            self.message.setText(str(exc))

    def refocus(self, source_id: str, timestamp: int) -> None:
        candidates = []
        for row in range(self.montage.count()):
            index = self.montage.item(row).data(Qt.ItemDataRole.UserRole)
            d = self.session.plan.decisions[index]
            if d.source_id == source_id:
                distance = (
                    0
                    if d.source_start_us <= timestamp < d.source_end_us
                    else 1 + min(abs(timestamp-d.source_start_us), abs(timestamp-d.source_end_us))
                )
                candidates.append((distance, row))
        if candidates:
            self.montage.setCurrentRow(min(candidates)[1])
            self.montage.scrollToItem(self.montage.currentItem())

    def history(self, redo: bool) -> None:
        focus = getattr(self, "edit_focus", (self.sources.currentData(), self.timeline.start))
        self.session.undo(redo)
        self.refresh()
        self.refocus(*focus)

    def fit_duration(self) -> None:
        try:
            self.session.fit_duration()
            self.refresh()
        except ValueError as exc:
            self.message.setText(str(exc))

    def toggle_play(self) -> None:
        if self.scrub_timer.isActive():
            self.scrub_timer.stop()
            self.flush_scrub()
        if self.playback.want_play:
            self.playback.pause()
        else:
            if not self.context_ready:
                # Bind the visible selection again, never resume an older episode window.
                position = self.playback.player.position()
                self.select_range(round(self.start.value() * 1e6), round(self.end.value() * 1e6))
                self.playback.limit_frames = True
                if self.playback.start <= position < self.playback.end - 1:
                    self.playback.seek(position)
            self.playback.play()

    def refresh_audio(self) -> None:
        self.playback.audio.setVolume(self.volume_state.level / 100)
        self.playback.audio.setMuted(
            (not self.context_ready and not self.sequence.enabled) or self.volume_state.silent
            or (self.music_track is not None and self.music_track.independent))

    def play_state(self, playing: bool) -> None:
        self.play.setText(tr("Пауза") if playing else tr("Просмотреть выделенное"))

    def stop_context(self) -> None:
        self.context_serial += 1
        self.context_ready = False
        self.preparer.cancel()
        self.playback.pause()
        self.play.setEnabled(True)
        self.context.setText(tr("Посмотреть с соседними фрагментами"))
        self.refresh_audio()

    def preview_context(self) -> None:
        from sculptcut.shorts_layout import SHORTS_PRESETS

        self.sequence.stop()
        if self.context_ready or not self.play.isEnabled():
            self.select_range(self.timeline.start, self.timeline.end)
            return
        self.context_serial += 1
        try:
            preview = self.pending_selection()
        except ValueError as exc:
            self.message.setText(str(exc))
            return
        self.preview_plan = preview.plan
        a = round(self.start.value() * 1e6)
        sid = self.sources.currentData()
        active = [i for i, d in enumerate(preview.plan.decisions) if d.output_frame_count]
        chosen = next(
            (
                i
                for i in active
                if preview.plan.decisions[i].source_id == sid
                and preview.plan.decisions[i].source_start_us
                <= a
                < preview.plan.decisions[i].source_end_us
            ),
            active[0],
        )
        index = active.index(chosen)
        rows = active[max(0, index - 1) : index + 2]
        scenes = []
        for i in rows:
            d = preview.plan.decisions[i]
            source = next(s for s in preview.project.sources if s.id == d.source_id)
            proxy = proxy_path(self.root, self.session.project, source)
            scenes.append(
                PreviewScene(
                    i,
                    proxy if proxy.is_file() and preview.project.settings.preset
                    not in SHORTS_PRESETS else source.path,
                    d.source_start_us // 1000,
                    d.source_end_us // 1000,
                    d.output_frame_count,
                    d,
                )
            )
        if any(preview.plan.decisions[i].action == "MONTAGE" for i in rows):
            self.message.setText(
                tr("Для проверки выберите рабочую сцену перед финальным стоп-кадром.")
            )
            return
        self.playback.pause()
        self.play.setEnabled(False)
        self.context.setText(tr("Отменить подготовку"))
        plan, settings = preview.plan, preview.project.settings
        offset = timeline_start(preview.project, plan, preview.original.leading)
        offset += sum(d.output_frame_count for d in plan.decisions[: rows[0]])
        total = timeline_frames(preview.project, plan,
                                preview.original.leading, preview.original.trailing)
        audio = (
            PreviewAudio(
                settings.music_path,
                offset / plan.output_fps,
                total / plan.output_fps,
                settings.music_loop,
                settings.music_fade_seconds,
            )
            if settings.music_path
            else None
        )
        self.message.setText(tr("Готовим короткий просмотр склеек…"))
        size = (540, 960) if settings.preset in SHORTS_PRESETS else (960, 540)
        self.preparer.request(shorts_scenes(tuple(scenes), preview.project, size, plan.output_fps),
                              plan.output_fps, size, audio)

    def prepared(self, clip: ContextClip) -> None:
        if self.closed or self.play.isEnabled():
            return
        self.context_ready = True
        self.play.setEnabled(True)
        self.context.setText(tr("Остановить просмотр склеек"))
        self.playback.player.setPlaybackRate(1)
        self.playback.select(clip.path, 0, clip.spans[-1][1], 0)
        self.refresh_audio()
        self.message.setText(tr("Просмотр склеек с музыкой — временный фрагмент."))
        self.playback.play()

    def finished_preview(self) -> None:
        if self.sequence.enabled:
            return
        if self.context_ready:
            serial = self.context_serial
            QTimer.singleShot(
                0,
                lambda: (
                    self.select_range(self.timeline.start, self.timeline.end)
                    if not self.closed and serial == self.context_serial
                    else None
                ),
            )

    def thumbnail(self, key: ImageKey, image: QImage) -> None:
        if (not self.closed and key == self.picture_key and not self.context_ready
                and not self.sequence.enabled):
            self.show_image(image)

    def scrub_frame_ready(self, key: ImageKey, image: QImage) -> None:
        if not self.closed and not self.sequence.enabled and not self.context_ready:
            self.show_image(image)

    def playback_image(self, image: QImage, stamp: int) -> None:
        if (not self.compact or self.sequence.enabled or self.context_ready
                or self.scrub_frames.wanted is None):
            self.show_image(image)

    def show_image(self, image: QImage) -> None:
        self.frame_image = image
        self.preview_image.emit(image)
        if not self.compact:
            self.fit_picture()

    def fit_picture(self) -> None:
        if not self.frame_image.isNull():
            self.picture.setPixmap(
                QPixmap.fromImage(self.frame_image).scaled(
                    self.picture.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if not self.loading:
            self.fit_picture()

    def fail(self, message: str) -> None:
        if not self.closed:
            self.stop_context()
            self.message.setText(message)
            self.message.show()

    def shutdown(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.hover_keys is not None:
            self.hover_keys.shutdown()
        if self.music_track is not None:
            self.music_track.shutdown()
        if self.loading:
            if self.loader in _loaders:
                self.loader.requestInterruption()
            return
        self.playback.shutdown()
        self.sequence.shutdown()
        self.scrub_timer.stop()
        self.preparer.shutdown()
        self.navigation_timer.stop()
        self.cards_timer.stop()
        self.cards.shutdown()
        self.images.shutdown()
        self.scrub_frames.shutdown()

    def done(self, result: int) -> None:
        self.shutdown()
        super().done(result)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.shutdown()
        super().closeEvent(event)
