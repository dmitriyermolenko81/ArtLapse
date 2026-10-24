"""One edit history for timing, framing and output cuts."""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QSize
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QPushButton

from sculptcut.domain import Override, Project, Roi
from sculptcut.i18n import tr
from sculptcut.project import load_project, save_project
from sculptcut.ui_icons import ui_icon

if TYPE_CHECKING:
    from sculptcut.ui import MainWindow


@dataclass(frozen=True)
class Framing:
    crop: Roi
    customized: bool
    squares: dict[Path, Roi]
    webcams: dict[Path, Roi | None]
    webcam_enabled: bool
    frame_mode: str
    shorts_layout: str


@dataclass(frozen=True)
class Timing:
    preset: str
    target: float
    normal: float
    shorts: float
    last_timelapse: str
    last_shorts: str


class WindowHistory:
    def __init__(self, window: "MainWindow") -> None:
        self.window = window
        self.history = window.edit_history
        self.undo_action = QAction(tr("Отменить"), window)
        self.redo_action = QAction(tr("Повторить"), window)
        self.undo_action.setShortcut(QKeySequence("Ctrl+Z"))
        self.redo_action.setShortcuts([QKeySequence("Ctrl+Y"), QKeySequence("Ctrl+Shift+Z")])
        self.undo_action.triggered.connect(self.undo)
        self.redo_action.triggered.connect(self.redo)
        window.addActions([self.undo_action, self.redo_action])
        self.undo_button = QPushButton()
        self.redo_button = QPushButton()
        self.undo_button.setIcon(ui_icon("undo"))
        self.redo_button.setIcon(ui_icon("redo"))
        for button, name, callback in (
            (self.undo_button, tr("Отменить действие"), self.undo),
            (self.redo_button, tr("Повторить действие"), self.redo),
        ):
            button.setAccessibleName(name)
            button.setFixedSize(QSize(36, 36))
            button.setIconSize(QSize(20, 20))
            button.setProperty("actionRole", "utility")
            button.setStyleSheet("QPushButton { padding:0; }")
            button.clicked.connect(callback)
        self.history.changed = self.refresh
        window.duration_card.edited.connect(self.record_duration)
        self.pending_timing = self.timing()
        window.preset.about_to_activate.connect(self.begin_timing)
        window.preset.activated.connect(self.finish_timing)
        self.pending_framing = self.framing()
        for selector in (window.frame_mode, window.shorts_layout):
            selector.about_to_activate.connect(self.begin_framing)
            selector.activated.connect(self.finish_framing)
        window.output_editor.record_cuts = self.record_cuts
        window.output_editor.undo_handler = self.undo
        window.output_editor.redo_handler = self.redo
        window.folder.textChanged.connect(self.history.clear)
        for signal in (window.sources.model().rowsInserted, window.sources.model().rowsRemoved,
                       window.sources.model().rowsMoved):
            signal.connect(self.history.clear)
        self.refresh()

    def refresh(self) -> None:
        available = self.window.job is None
        for action, button, label, verb, key in (
            (self.undo_action, self.undo_button, self.history.undo_label, tr("Отменить"), "Ctrl+Z"),
            (
                self.redo_action,
                self.redo_button,
                self.history.redo_label,
                tr("Повторить"),
                "Ctrl+Y",
            ),
        ):
            text = f"{verb}: {tr(label)} ({key})" if label else f"{verb} ({key})"
            action.setEnabled(available and bool(label))
            button.setEnabled(available and bool(label))
            action.setText(text)
            button.setToolTip(text)
        editor = self.window.output_editor
        editor.update_actions()
        editor.undo_button.setEnabled(self.undo_action.isEnabled())
        editor.redo_button.setEnabled(self.redo_action.isEnabled())
        editor.undo_button.setToolTip(self.undo_button.toolTip())
        editor.redo_button.setToolTip(self.redo_button.toolTip())

    def undo(self) -> None:
        self.replay(False)

    def redo(self) -> None:
        self.replay(True)

    def replay(self, redo: bool) -> None:
        if self.window.job is not None:
            return
        label = self.history.redo_label if redo else self.history.undo_label
        if not label:
            return
        try:
            self.window.player.pause()
            if redo:
                self.history.redo()
            else:
                self.history.undo()
            self.window.status.setText((tr("Повторено: ") if redo else tr("Отменено: ")) + label)
            self.window.autosave_draft()
        except (OSError, ValueError) as exc:
            self.window.show_error(str(exc))

    def timing(self) -> Timing:
        w = self.window
        return Timing(w.preset.currentData(), w.target.value(),
                      w.normal_minutes, w.shorts_seconds,
                      w.last_timelapse_preset, w.last_shorts_preset)

    def begin_timing(self) -> None:
        self.pending_timing = self.timing()

    def finish_timing(self) -> None:
        self.record_timing(self.pending_timing)

    def record_timing(self, before: Timing) -> None:
        self.history.record(tr("Скорость и длительность"), before, self.timing(), self.apply_timing)

    def record_duration(self, before: float, after: float) -> None:
        if before == after:
            return
        state = self.timing()
        self.record_timing(Timing(state.preset, before, state.normal, state.shorts,
                                  state.last_timelapse, state.last_shorts))

    def apply_timing(self, state: Timing) -> None:
        w = self.window
        w.preset.setCurrentIndex(w.preset.findData(state.preset))
        w.target.setValue(state.target)
        w.normal_minutes, w.shorts_seconds = state.normal, state.shorts
        w.last_timelapse_preset, w.last_shorts_preset = state.last_timelapse, state.last_shorts
        w.invalidate_episodes()

    def begin_framing(self) -> None:
        self.pending_framing = self.framing()

    def finish_framing(self) -> None:
        self.record_framing(self.pending_framing)

    def framing(self) -> Framing:
        w = self.window
        return Framing(w.crop_roi, w.crop_customized, dict(w.square_rois),
                       dict(w.webcam_rois), w.webcam_enabled.isChecked(),
                       w.frame_mode.currentData(), w.shorts_layout.currentData())

    def record_framing(self, before: Framing) -> None:
        self.history.record(tr("Рамка кадра"), before, self.framing(), self.apply_framing)

    def apply_framing(self, state: Framing) -> None:
        w = self.window
        w.crop_roi, w.crop_customized = state.crop, state.customized
        w.square_rois, w.webcam_rois = dict(state.squares), dict(state.webcams)
        w.webcam_enabled.blockSignals(True)
        w.webcam_enabled.setChecked(state.webcam_enabled)
        w.webcam_enabled.blockSignals(False)
        w.frame_mode.setCurrentIndex(w.frame_mode.findData(state.frame_mode))
        w.shorts_layout.setCurrentIndex(w.shorts_layout.findData(state.shorts_layout))
        w.update_aspect_captions()
        w.invalidate_episodes()

    def record_cuts(self, before: list[tuple[int, int]], after: list[tuple[int, int]]) -> None:
        w = self.window
        video = Path(w.player.source().toLocalFile())
        stat = video.stat()
        identity = stat.st_size, stat.st_mtime_ns
        duration = w.output_editor.duration

        def apply(cuts: list[tuple[int, int]]) -> None:
            current = video.stat()
            if (current.st_size, current.st_mtime_ns) != identity:
                raise ValueError(tr("Видео изменилось. Откройте его заново перед редактированием."))
            if Path(w.player.source().toLocalFile()) != video:
                w.open_video(video, tr("Редактирование ролика"))
            w.player.pause()
            w.output_editor.set_duration(duration // 1000)
            w.output_editor.show()
            w.set_output_editor_button(True)
            w.output_editor.set_cuts(cuts)

        self.history.record(tr("Вырезка"), list(before), list(after), apply)

    def record_episode(self, root: Path, before: Project, after: Project, label: str) -> None:
        expected = list(after.overrides)
        def identity() -> bytes | None:
            path = root / "render_plan.json"
            return sha256(path.read_bytes()).digest() if path.is_file() else None

        plan_identity = identity()

        def apply(overrides: list[Override]) -> None:
            nonlocal expected
            project = load_project(root)
            if (project.overrides != expected
                    or project.sources != before.sources
                    or identity() != plan_identity):
                raise ValueError(
                    tr("Монтаж изменился. Откройте проект заново перед отменой эпизодов.")
                )
            project = Project.model_validate(project.model_dump() | {"overrides": overrides})
            save_project(root, project)
            expected = list(overrides)
            panel = self.window.episode_panel
            if panel is not None and not panel.closed and panel.root == root:
                panel.editor.load(root)

        self.history.record(label, list(before.overrides), list(after.overrides), apply)
