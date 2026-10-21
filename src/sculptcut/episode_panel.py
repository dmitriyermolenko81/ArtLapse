"""Visual correction of an existing automatic edit, without rebuilding analysis."""

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import (
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    QRect,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QColor,
    QIcon,
    QImage,
    QPainter,
    QPixmap,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListView,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from sculptcut.domain import Project
from sculptcut.episode_editor import EpisodeEditor
from sculptcut.episode_images import EpisodeImages, ImageKey, image_key
from sculptcut.episode_music import EpisodeMusic
from sculptcut.episode_playback import EpisodePlayback
from sculptcut.episode_timeline import EpisodeTimeline
from sculptcut.i18n import tr
from sculptcut.intro import output_frame_count
from sculptcut.overrides import apply_overrides
from sculptcut.planning import RenderDecision, RenderPlan, validate_plan
from sculptcut.playback_volume import PlaybackVolume, VolumeControl
from sculptcut.project import load_project
from sculptcut.time_format import format_time
from sculptcut.ui_icons import ui_icon
from sculptcut.ui_theme import apply_dialog_theme, dialog_header
from sculptcut.variants import read_variant


def episode_export(root: Path) -> tuple[Project, RenderPlan]:
    project = load_project(root)
    base = RenderPlan.model_validate_json((root / "render_plan.json").read_text(encoding="utf-8"))
    # A successful music export advances the *next* music seed in project.json.
    # Match the original plan snapshot instead of applying next-run settings.
    for video in sorted((root / "renders").glob("*.mp4"),
                        key=lambda p: p.stat().st_mtime_ns, reverse=True):
        try:
            state = read_variant(video)
            if RenderPlan.model_validate(state.get("plan")) != base:
                continue
            original = Project.model_validate(state.get("project"))
            project = Project.model_validate(project.model_dump() | {
                "settings": original.settings, "rois": original.rois,
            })
            break
        except (OSError, ValueError):
            continue
    validate_plan(project, base)
    return project, apply_overrides(project, base)


class EpisodeCardDelegate(QStyledItemDelegate):
    def paint(self, painter: QPainter, option: QStyleOptionViewItem,
              index: QModelIndex | QPersistentModelIndex) -> None:
        painter.save()
        rect = option.rect.adjusted(3, 3, -3, -3)  # type: ignore[attr-defined]
        selected = bool(option.state & QStyle.StateFlag.State_Selected)  # type: ignore[attr-defined]
        painter.setPen(QColor("#76b4f4") if selected else QColor("#354354"))
        painter.setBrush(QColor("#213451") if selected else QColor("#202936"))
        painter.drawRoundedRect(rect, 6, 6)
        thumb = QRect(rect.center().x() - 56, rect.top() + 5, 112, 63)
        painter.fillRect(thumb, QColor("#0c1118"))
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(icon, QIcon) and not icon.isNull():
            icon.paint(painter, thumb)
        lines = str(index.data(Qt.ItemDataRole.DisplayRole) or "").split("\n", 1)
        for line, text in enumerate(lines):
            painter.setPen(QColor("#e0eaf7" if line == 0 else "#9aabc0"))
            painter.drawText(QRect(rect.left() + 4, rect.top() + 70 + line * 16,
                                   rect.width() - 8, 16), Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()


def episode_toggle(text: str) -> QPushButton:
    button = QPushButton(text)
    button.setCheckable(True)
    button.setFixedSize(110, 30)
    button.setIconSize(QSize(14, 14))
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setStyleSheet("""
        QPushButton { background:#181d26; color:#9aabc0; border:1px solid #354354;
                      border-radius:15px; padding:3px 10px; }
        QPushButton:hover { background:#273547; border-color:#6e8fb6; color:#e0eaf7; }
        QPushButton:checked { background:#243e60; border-color:#76b4f4; color:#e0eaf7; }
        QPushButton:pressed { background:#35547a; }
        QPushButton:focus { border:2px solid #bcd1ff; }
        QPushButton:disabled { color:#637083; border-color:#283241; }
    """)
    button.toggled.connect(lambda checked: button.setIcon(ui_icon("check") if checked else QIcon()))
    return button


class EpisodePanel(QDialog):
    export_requested = Signal()

    def __init__(self, root: Path, parent: QWidget, *,
                 volume_state: PlaybackVolume | None = None) -> None:
        super().__init__(parent)
        apply_dialog_theme(self)
        self.root = root
        self.project, _ = episode_export(root)
        self.setWindowTitle(tr("Эпизоды монтажа"))
        self.resize(1000, 650)
        self.setMinimumSize(760, 620)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 18)
        layout.setSpacing(8)
        help_text = dialog_header(tr("Эпизоды монтажа"), "episodes_music")
        help_text.setToolTip(
            tr("Пробел — просмотр, светлые ручки — границы, ПКМ — действия.\n"
            "Колесо — масштаб, Shift+колесо — сдвиг. Правки сохраняются в проекте. "
            "Общие для всех режимов, кроме «Просто ускорение». "
            "Готовое видео обновит «Создать исправленную копию».")
        )
        help_text.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        layout.addWidget(help_text)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.summary.setStyleSheet("background:#181d26; border-radius:6px; padding:6px;")
        layout.addWidget(self.summary)
        source_row = QHBoxLayout()
        source_row.addWidget(QLabel(tr("Исходник")))
        self.sources = QComboBox()
        self.sources.setProperty("controlRole", "menu")
        self.sources.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon,
        )
        self.sources.setMinimumContentsLength(12)
        self.sources.setMinimumHeight(32)
        for source in self.project.sources:
            self.sources.addItem(source.path.name, source.id)
        source_row.addWidget(self.sources, 1)
        source_row.addWidget(QLabel(tr("Исходник · 1×")))
        layout.addLayout(source_row)
        self.picture = QLabel(tr("Выберите эпизод — здесь появится кадр исходника"))
        self.picture_image = QImage()
        self.picture.setMinimumHeight(110)
        self.picture.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.picture.setStyleSheet("background:#0c1118; border-radius:6px;")
        layout.addWidget(self.picture, 1)
        self.playback = EpisodePlayback(self)
        self.volume_state = (
            volume_state if volume_state is not None else PlaybackVolume(parent=self)
        )
        self.volume_state.bind(self.playback.audio)
        self.volume_control = VolumeControl(self.volume_state)
        self.video_active = False
        self.playback.image_ready.connect(self.video_frame)
        self.playback.position_changed.connect(self.playback_position)
        self.playback.playing_changed.connect(self.playback_state)
        self.playback.failed.connect(self.playback_failed)
        playback_row = QHBoxLayout()
        self.play_button = QPushButton(tr("▶ Воспроизвести"))
        self.play_button.setEnabled(False)
        self.play_button.clicked.connect(self.toggle_playback)
        self.previous_button = QPushButton()
        self.next_button = QPushButton()
        self.previous_button.setIcon(ui_icon("previous"))
        self.next_button.setIcon(ui_icon("next"))
        for button, name, direction in (
            (self.previous_button, tr("Предыдущий эпизод"), -1),
            (self.next_button, tr("Следующий эпизод"), 1),
        ):
            button.setAccessibleName(name)
            button.setToolTip(name + tr(" · с учётом фильтров"))
            button.setFixedSize(34, 32)
            button.setStyleSheet("QPushButton { padding:3px; }")
            button.setIconSize(QSize(20, 20))
            button.clicked.connect(
                lambda checked=False, step=direction: self.navigate_episode(step)
            )
        self.restart_button = QPushButton(tr("К началу"))
        self.restart_button.setToolTip(tr("К началу выбранного эпизода"))
        self.restart_button.setEnabled(False)
        self.restart_button.clicked.connect(self.restart_episode)
        self.loop_button = episode_toggle(tr("По кругу"))
        self.loop_button.toggled.connect(lambda value: setattr(self.playback, "loop", value))
        self.music_button = episode_toggle(tr("Волна"))
        # Keep the diagnostic available internally, without exposing manual
        # soundtrack inspection in the automated editing workflow.
        self.music_button.setParent(self)
        self.music_button.hide()
        self.music_button.setEnabled(bool(self.project.settings.music_path
                                          or self.project.settings.music_folder))
        self.music_button.setToolTip(tr("Музыка и склейки по времени готового ролика"))
        self.music_button.toggled.connect(self.toggle_music)
        playback_row.addWidget(self.previous_button)
        playback_row.addWidget(self.play_button)
        playback_row.addWidget(self.next_button)
        playback_row.addWidget(self.restart_button)
        playback_row.addWidget(self.loop_button)
        playback_row.addStretch(1)
        playback_row.addWidget(self.volume_control)
        layout.addLayout(playback_row)
        play_action = QAction(self)
        play_action.setShortcut("Space")
        play_action.triggered.connect(self.toggle_playback)
        self.addAction(play_action)
        self.time_label = QLabel(tr("Время указано в исходнике"))
        self.time_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.editor = EpisodeEditor()
        self.editor.show_cards = True
        self.editor.validate_edits = True
        self.editor.context_settings = self.project.settings
        self.editor.setItemDelegate(EpisodeCardDelegate(self.editor))
        self.editor.setViewMode(QListView.ViewMode.IconMode)
        self.editor.setFlow(QListView.Flow.LeftToRight)
        self.editor.setWrapping(False)
        self.editor.setFixedHeight(134)
        self.editor.setUniformItemSizes(True)
        self.editor.setWordWrap(True)
        self.editor.setMovement(QListView.Movement.Static)
        self.editor.setDragEnabled(False)
        self.editor.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.editor.setIconSize(QSize(112, 63))
        self.timeline = EpisodeTimeline(self.editor)
        self.music = EpisodeMusic(root, self)
        self.music.hide()
        self.music.episode_requested.connect(self.select_music_episode)
        filters = QHBoxLayout()
        filters.addWidget(self.time_label)
        filters.addStretch(1)
        self.show_kept = episode_toggle(tr("В ролике"))
        self.show_dropped = episode_toggle(tr("Пропуск"))
        for checkbox in (self.show_kept, self.show_dropped):
            checkbox.setChecked(True)
            checkbox.setToolTip(tr("Фильтр отображения — монтаж и экспорт не меняются"))
            checkbox.toggled.connect(self.apply_filter)
            filters.addWidget(checkbox)
        self.shown_label = QLabel()
        self.shown_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        filters.addWidget(self.shown_label)
        layout.addLayout(filters)
        layout.addWidget(self.timeline)
        layout.addWidget(self.music)
        layout.addWidget(self.editor)
        self.message = QLabel(
            tr("Серые эпизоды исключены из ролика. Стоп-кадры имеют фиксированные границы.")
        )
        self.message.setWordWrap(True)
        self.message.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.message)
        actions = QHBoxLayout()
        self.undo_button = QPushButton(tr("Отменить"))
        self.redo_button = QPushButton(tr("Повторить"))
        actions.addWidget(self.undo_button)
        actions.addWidget(self.redo_button)
        actions.addStretch(1)
        self.export_button = QPushButton(tr("Создать исправленную копию"))
        self.export_button.setObjectName("primary")
        self.export_button.setToolTip(
            tr("Использует длительность, музыку и рамку сохранённого монтажа")
        )
        self.export_button.clicked.connect(self.export_requested.emit)
        actions.addWidget(self.export_button)
        close = QPushButton(tr("Закрыть"))
        close.clicked.connect(self.reject)
        actions.addWidget(close)
        layout.addLayout(actions)
        self.popup = QWidget(self, Qt.WindowType.ToolTip)
        self.popup.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.popup.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        popup_layout = QVBoxLayout(self.popup)
        self.popup_image = QLabel(tr("Загрузка кадра…"))
        self.popup_image.setFixedSize(240, 135)
        self.popup_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.popup_time = QLabel()
        popup_layout.addWidget(self.popup_image)
        popup_layout.addWidget(self.popup_time)
        self.cards = EpisodeImages(self)
        self.frames = EpisodeImages(self, limit=2, width=480, intermediate=True)
        self.cards.ready.connect(self.card_ready)
        self.frames.ready.connect(self.frame_ready)
        self.frames.failed.connect(self.frame_failed)
        self.cards.failed.connect(self.message.setText)
        self.visible_timer = QTimer(self)
        self.visible_timer.setSingleShot(True)
        self.visible_timer.setInterval(60)
        self.visible_timer.timeout.connect(self.request_visible)
        self.editor.horizontalScrollBar().valueChanged.connect(lambda _: self.visible_timer.start())
        self.editor.loaded.connect(self.refresh_cards)
        self.editor.preview_requested.connect(self.preview_episode)
        self.editor.currentItemChanged.connect(lambda *_: self.update_navigation())
        self.editor.failed.connect(self.message.setText)
        self.editor.changed.connect(lambda _: self.message.setText(
            tr("Правка сохранена. Нажмите «Создать исправленную копию».")
        ))
        self.timeline.hover_requested.connect(self.preview_boundary)
        self.timeline.hover_finished.connect(self.popup.hide)
        self.timeline.dragging.connect(self.cards.set_paused)
        self.timeline.dragging.connect(self.drag_playback)
        self.timeline.seek_requested.connect(self.seek_playback)
        self.sources.currentIndexChanged.connect(self.select_source)
        self.card_keys: dict[int, ImageKey] = {}
        self.requested: ImageKey | None = None
        self.closed = False
        self.active_source = ""
        self.history_connections: list[tuple[QAction, Callable[[], None]]] = []
        self.editor.load(root)

    def bind_history(self, undo: QAction, redo: QAction) -> None:
        for button, action in ((self.undo_button, undo), (self.redo_button, redo)):
            button.clicked.connect(action.trigger)
            local = QAction(self)
            local.setShortcuts(action.shortcuts())
            local.triggered.connect(action.trigger)
            self.addAction(local)

            def sync(button: QPushButton = button, action: QAction = action,
                     local: QAction = local) -> None:
                button.setEnabled(action.isEnabled())
                button.setToolTip(action.text())
                local.setEnabled(action.isEnabled())

            action.changed.connect(sync)
            self.history_connections.append((action, sync))
            sync()

    def refresh_cards(self) -> None:
        had_selection = self.play_button.isEnabled()
        self.playback.pause()
        self.play_button.setEnabled(False)
        self.restart_button.setEnabled(False)
        self.playback.end = 0
        self.card_keys.clear()
        for i in range(self.editor.count()):
            item = self.editor.item(i)
            item.setSizeHint(QSize(164, 112))
            d: RenderDecision = item.data(Qt.ItemDataRole.UserRole)
            source = next(s for s in self.project.sources if s.id == d.source_id)
            item.setToolTip(
                f"{source.path.name} · {format_time(d.source_start_us / 1e6, 2)} — "
                f"{format_time(d.source_end_us / 1e6, 2)}"
            )
            if d.action in ("HOLD", "MONTAGE"):
                label = tr("Стоп-кадр") if d.action == "HOLD" else tr("Набор кадров")
                item.setText(label + "\n" + item.text().split("\n")[-1])
        self.select_source()
        self.update_summary()
        self.refresh_music()
        self.editor.show()
        self.visible_timer.start()
        if had_selection:
            QTimer.singleShot(0, self.restore_playback)

    def restore_playback(self) -> None:
        if self.closed:
            return
        decision = self.timeline.selected_decision()
        if decision is None:
            self.timeline.set_position(self.playback.start)
            decision = self.timeline.selected_decision()
        if decision is not None:
            self.preview_episode(self.timeline.url.toLocalFile(),
                                 decision.source_start_us // 1000,
                                 decision.source_end_us // 1000)

    def select_source(self) -> None:
        source = next(s for s in self.project.sources if s.id == self.sources.currentData())
        self.timeline.set_source(QUrl.fromLocalFile(str(source.path.resolve())))
        self.popup.hide()
        if self.active_source != source.id:
            self.playback.pause()
            self.playback.player.setSource(QUrl())
            self.playback.pending = None
            self.playback.end = 0
            self.video_active = False
            self.play_button.setEnabled(False)
            self.restart_button.setEnabled(False)
            self.active_source = source.id
            self.requested = None
            self.frames.request([])
            self.clear_picture(tr("Выберите эпизод — здесь появится кадр исходника"))
            self.popup_image.setText(tr("Загрузка кадра…"))
        self.apply_filter()

    def apply_filter(self) -> None:
        source_id = self.sources.currentData()
        shown, total = 0, 0
        for i in range(self.editor.count()):
            d: RenderDecision = self.editor.item(i).data(Qt.ItemDataRole.UserRole)
            matches = (self.show_dropped.isChecked() if d.action == "DROP"
                       else self.show_kept.isChecked())
            visible = d.source_id == source_id and matches
            total += int(d.source_id == source_id)
            shown += int(visible)
            self.editor.setRowHidden(i, not visible)
            if not visible:
                self.editor.item(i).setIcon(QIcon())
        self.shown_label.setText(tr(f"Показано: {shown} из {total}"))
        self.shown_label.setToolTip(tr("Эпизоды выбранного исходника"))
        if self.timeline.selected_decision() is None:
            self.playback.pause()
            self.playback.end = 0
            self.play_button.setEnabled(False)
            self.restart_button.setEnabled(False)
            self.timeline.selected = -1
            self.editor.clearSelection()
            self.popup.hide()
            self.requested = None
            self.frames.request([])
            self.video_active = False
            self.clear_picture(
                tr("Выберите эпизод") if shown else tr("Нет эпизодов по выбранным фильтрам")
            )
            self.time_label.setText(tr("Время указано в исходнике"))
        self.timeline.update()
        self.update_navigation()
        self.music.select_episode(self.timeline.selected)
        self.card_keys.clear()
        self.visible_timer.start()

    def update_summary(self) -> None:
        decisions: list[RenderDecision] = [
            self.editor.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.editor.count())
        ]
        kept = [d for d in decisions if d.action != "DROP"]
        dropped = [d for d in decisions if d.action == "DROP"]
        def duration(items: list[RenderDecision]) -> str:
            return format_time(sum(d.source_end_us - d.source_start_us for d in items) / 1e6, 1)
        output = (output_frame_count(self.project.settings, self.editor.plan.output_fps,
                                     self.editor.plan.actual_frame_count)
                  / self.editor.plan.output_fps
                  if self.editor.plan is not None else 0)
        self.summary.setText(
            tr(f"Весь проект · Всего: {len(decisions)} эп. · {duration(decisions)} исходников\n"
            f"В ролике: {len(kept)} эп. · {duration(kept)} исходников "
            f"→ {format_time(output, 1)} в готовом ролике   |   "
            f"Пропуск: {len(dropped)} эп. · {duration(dropped)} исходников")
        )

    def toggle_music(self, checked: bool) -> None:
        self.music.setVisible(checked)
        if checked:
            self.refresh_music()
            QTimer.singleShot(0, self.fit_music_height)

    def fit_music_height(self) -> None:
        if self.closed or not self.music_button.isChecked():
            return
        layout = self.layout()
        if layout is not None:
            layout.activate()
            required = layout.minimumSize().height()
            self.resize(self.width(), max(self.height(), required))

    def refresh_music(self) -> None:
        if not self.music_button.isChecked() or self.editor.plan is None:
            return
        decisions = [self.editor.item(i).data(Qt.ItemDataRole.UserRole)
                     for i in range(self.editor.count())]
        plan = self.editor.plan.model_copy(update={"decisions": decisions})
        self.music.set_plan(self.project.settings, plan)
        self.music.select_episode(self.timeline.selected)

    def select_music_episode(self, row: int) -> None:
        if row >= self.editor.count():
            return
        item = self.editor.item(row)
        d: RenderDecision = item.data(Qt.ItemDataRole.UserRole)
        if d.action == "DROP":
            return
        # Music always shows the full output, independently of source filters.
        self.show_kept.setChecked(True)
        index = self.sources.findData(d.source_id)
        self.sources.setCurrentIndex(index)
        self.editor.setCurrentItem(item)
        self.editor.scrollToItem(item)
        self.editor.preview_episode(item)

    def key_for(self, decision: RenderDecision) -> ImageKey:
        source = next(s for s in self.project.sources if s.id == decision.source_id)
        time = (decision.source_start_us if decision.action == "HOLD" else
                (decision.source_start_us + decision.source_end_us) // 2)
        if decision.sample_times_us:
            time = decision.sample_times_us[0]
        if decision.action == "HOLD" and decision.provenance in (
                "user:final_tail", "user:shorts_final"):
            frame_us = (1_000_000 * source.fps_den + source.fps_num - 1) // source.fps_num
            time = max(decision.source_start_us, decision.source_end_us - frame_us)
        return image_key(source.path, time)

    def request_visible(self) -> None:
        if self.closed or not self.isVisible():
            return
        viewport = QRect(self.editor.viewport().rect())
        keys = []
        try:
            for i in range(self.editor.count()):
                item = self.editor.item(i)
                if (not self.editor.isRowHidden(i)
                        and self.editor.visualItemRect(item).intersects(viewport)):
                    key = self.key_for(item.data(Qt.ItemDataRole.UserRole))
                    self.card_keys[i] = key
                    keys.append(key)
                elif not item.icon().isNull():
                    # QListWidget icons own pixmaps too: keep off-screen cards
                    # from bypassing the bounded image cache.
                    item.setIcon(QIcon())
            self.cards.request(keys)
        except OSError as exc:
            self.message.setText(tr(str(exc)))

    def card_ready(self, key: ImageKey, image: QImage) -> None:
        for i, requested in self.card_keys.items():
            if requested == key and i < self.editor.count():
                self.editor.item(i).setIcon(QIcon(QPixmap.fromImage(image)))

    def preview_episode(self, path: str, start: int, end: int) -> None:
        self.popup.hide()
        self.timeline.set_position(start)
        item = self.editor.currentItem()
        if item is not None and not self.timeline.scrubbing:
            d: RenderDecision = item.data(Qt.ItemDataRole.UserRole)
            row = self.editor.row(item)
            if (not self.editor.isRowHidden(row) and d.source_id == self.timeline.source_id
                    and start == d.source_start_us // 1000 and end == d.source_end_us // 1000):
                self.timeline.selected = row
        decision = self.timeline.selected_decision()
        if decision is None:
            return
        if decision is not None:
            self.playback.select(Path(path), decision.source_start_us // 1000,
                                 decision.source_end_us // 1000, start)
            self.play_button.setEnabled(True)
            self.restart_button.setEnabled(True)
            span = decision.source_end_us - decision.source_start_us
            margin = max(500_000, span // 4)
            if not self.timeline.scrubbing:
                self.timeline.set_view(decision.source_start_us - margin, span + 2 * margin)
        try:
            if decision is not None and decision.action in ("HOLD", "MONTAGE"):
                self.request_frame(self.key_for(decision)[1] // 1000)
            else:
                self.request_frame(start)
        except OSError as exc:
            self.frame_failed(str(exc))
        self.update_navigation()
        self.music.select_episode(self.timeline.selected)

    def adjacent_episode(self, step: int) -> int | None:
        rows = [i for i, _ in self.timeline.visible_decisions()]
        if not rows:
            return None
        if self.timeline.selected not in rows:
            return rows[0] if step > 0 else rows[-1]
        target = rows.index(self.timeline.selected) + step
        return rows[target] if 0 <= target < len(rows) else None

    def update_navigation(self) -> None:
        for button, step in ((self.previous_button, -1), (self.next_button, 1)):
            button.setEnabled(not self.closed and not self.timeline.drag
                              and self.adjacent_episode(step) is not None)

    def navigate_episode(self, step: int) -> None:
        if self.closed or self.timeline.drag:
            return
        row = self.adjacent_episode(step)
        if row is None:
            return
        item = self.editor.item(row)
        self.editor.setCurrentItem(item)
        self.editor.scrollToItem(item)
        self.editor.preview_episode(item)

    def toggle_playback(self) -> None:
        if not self.play_button.isEnabled() or self.timeline.drag:
            return
        if self.playback.want_play:
            self.playback.pause()
        else:
            self.video_active = True
            self.playback.play()

    def restart_episode(self) -> None:
        self.seek_playback(self.playback.start)

    def drag_playback(self, dragging: bool) -> None:
        if dragging:
            self.playback.pause()
        self.update_navigation()

    def seek_playback(self, milliseconds: int) -> None:
        self.playback.pause()
        if self.timeline.drag:
            return  # The boundary JPEG stays authoritative while trimming.
        self.video_active = True
        self.playback.seek(min(milliseconds, max(0, self.timeline.duration // 1000 - 1)))

    def playback_position(self, milliseconds: int) -> None:
        if self.closed or not self.video_active:
            return
        # Playback moves the cursor, not the selected edit or its handles.
        self.timeline.position = milliseconds * 1000
        self.timeline.update()
        self.time_label.setText(tr("Исходник · ") + format_time(milliseconds / 1000, 2))

    def playback_state(self, playing: bool) -> None:
        self.play_button.setText(tr("Ⅱ Пауза") if playing else tr("▶ Воспроизвести"))

    def playback_failed(self, message: str) -> None:
        self.video_active = False
        self.message.setText(tr("Не удалось воспроизвести исходник: ") + message)

    def video_frame(self, image: QImage, milliseconds: int) -> None:
        if self.closed or not self.video_active:
            return
        self.show_picture(image)

    def show_picture(self, image: QImage) -> None:
        self.picture_image = image
        self.fit_picture()

    def fit_picture(self) -> None:
        if self.picture_image.isNull():
            return
        self.picture.setPixmap(QPixmap.fromImage(self.picture_image).scaled(
            self.picture.size(), Qt.AspectRatioMode.KeepAspectRatio,
        ))

    def clear_picture(self, text: str) -> None:
        self.picture_image = QImage()
        self.picture.setText(text)

    def request_frame(self, milliseconds: int) -> None:
        self.video_active = False
        source = next(s for s in self.project.sources if s.id == self.sources.currentData())
        us = max(0, min(milliseconds * 1000, source.duration_us - 1))
        try:
            self.requested = image_key(source.path, us)
            self.time_label.setText(tr("Исходник · ") + format_time(us / 1e6, 2))
            self.frames.request([self.requested])
        except OSError as exc:
            self.frame_failed(str(exc))

    def preview_boundary(self, milliseconds: int, point: QPoint) -> None:
        self.request_frame(milliseconds)
        self.popup_time.setText(tr("Граница · ") + format_time(milliseconds / 1000, 2))
        self.popup.adjustSize()
        screen = self.screen().availableGeometry()
        x = max(screen.left(), min(point.x() - self.popup.width() // 2,
                                   screen.right() - self.popup.width()))
        self.popup.move(x, max(screen.top(), point.y() - self.popup.height() - 8))
        self.popup.show()

    def frame_ready(self, key: ImageKey, image: QImage) -> None:
        if (self.requested is None or key[0] != self.requested[0]
                or key[2:] != self.requested[2:]):
            return
        pixmap = QPixmap.fromImage(image)
        if not self.video_active:
            self.time_label.setText(tr("Кадр исходника · ") + format_time(key[1] / 1e6, 2))
            self.show_picture(image)
        self.popup_image.setPixmap(pixmap.scaled(240, 135, Qt.AspectRatioMode.KeepAspectRatio))
        self.popup_time.setText(tr("Кадр · ") + format_time(key[1] / 1e6, 2))

    def frame_failed(self, message: str) -> None:
        self.message.setText(tr(message))
        self.popup_image.setText(tr("Кадр недоступен"))

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if hasattr(self, "visible_timer"):
            self.visible_timer.start()
            self.fit_picture()

    def shutdown(self) -> None:
        self.closed = True
        self.music.shutdown()
        self.playback.shutdown()
        for action, callback in self.history_connections:
            action.changed.disconnect(callback)
        self.history_connections.clear()
        self.popup.hide()
        self.visible_timer.stop()
        self.cards.shutdown()
        self.frames.shutdown()

    def done(self, result: int) -> None:
        self.shutdown()
        super().done(result)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.shutdown()
        super().closeEvent(event)
