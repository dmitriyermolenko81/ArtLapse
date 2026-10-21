"""Compact source-ordered episode strip backed by non-destructive overrides."""

from pathlib import Path

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtGui import QColor, QCursor
from PySide6.QtWidgets import QListView, QListWidget, QListWidgetItem, QMenu

from sculptcut.domain import Override, Project, ProjectSettings
from sculptcut.i18n import tr
from sculptcut.overrides import apply_overrides
from sculptcut.planning import RenderDecision, RenderPlan
from sculptcut.project import load_project, save_project
from sculptcut.time_format import format_time
from sculptcut.ui_icons import ui_icon


def edit_episode(project: Project, decision: RenderDecision, speed: float | None) -> Project:
    """Replace this interval, retaining non-overlapping portions of older edits."""
    start, end = decision.source_start_us, decision.source_end_us
    edits = []
    for old in project.overrides:
        if old.source_id != decision.source_id or old.end_us <= start or old.start_us >= end:
            edits.append(old)
            continue
        if old.start_us < start:
            edits.append(old.model_copy(update={"end_us": start}))
        if old.end_us > end:
            edits.append(old.model_copy(update={"start_us": end}))
    edits.append(
        Override(
            source_id=decision.source_id,
            start_us=start,
            end_us=end,
            action="DROP" if speed is None else "SET_SPEED",
            speed=1 if speed is None else speed,
        )
    )
    return Project.model_validate(project.model_dump() | {"overrides": edits})


class EpisodeEditor(QListWidget):
    show_cards = False
    validate_edits = False
    edit_saved = Signal(object, object, str)
    loaded = Signal()
    changed = Signal(str)
    failed = Signal(str)
    preview_requested = Signal(str, int, int)

    def __init__(self) -> None:
        super().__init__()
        self.root: Path | None = None
        self.plan: RenderPlan | None = None
        self.context_settings: ProjectSettings | None = None
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(False)
        self.setFixedHeight(64)
        self.setSpacing(3)
        self.setStyleSheet("""
            QListWidget { background:#181d26; border:1px solid #303949;
                          border-radius:6px; padding:3px; }
            QListWidget::item { border:1px solid transparent; border-radius:4px; }
            QListWidget::item:hover { background:#252f40; }
            QListWidget::item:selected { background:#283d60; border-color:#6b94df; color:#ffffff; }
            QScrollBar:horizontal { background:#181d26; height:6px; margin:0; }
            QScrollBar::handle:horizontal { background:#52627b; border-radius:3px; min-width:24px; }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width:0; }
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background:none; }
        """)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.setToolTip(
            tr("ЛКМ — просмотр эпизода. ПКМ — оставить, удалить или замедлить. Серые — исключены.")
        )
        self.itemClicked.connect(self.preview_episode)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.context_menu)
        self.hide()

    def preview_episode(self, item: QListWidgetItem) -> None:
        if self.root is None:
            return
        try:
            d: RenderDecision = item.data(Qt.ItemDataRole.UserRole)
            project = load_project(self.root)
            source = next(s for s in project.sources if s.id == d.source_id)
            self.preview_requested.emit(
                str(source.path), d.source_start_us // 1000, d.source_end_us // 1000
            )
        except (OSError, ValueError, StopIteration) as exc:
            self.failed.emit(str(exc))

    def context_menu(self, position: QPoint) -> None:
        item = self.itemAt(position)
        if item is not None:
            self.setCurrentItem(item)
            self.choose(item)

    def load(self, root: Path) -> None:
        self.clear()
        self.hide()
        self.root, self.plan = root, None
        path = root / "render_plan.json"
        if not path.is_file():
            return
        try:
            self.plan = RenderPlan.model_validate_json(path.read_text(encoding="utf-8"))
            project = load_project(root)
            if self.context_settings is not None:
                project = project.model_copy(update={"settings": self.context_settings})
            try:
                self.plan = apply_overrides(project, self.plan)
            except ValueError as exc:
                if str(exc) != "Overrides drop all footage":
                    raise
                self.plan = self.plan.model_copy(
                    update={
                        "decisions": [
                            d.model_copy(update={"action": "DROP", "output_frame_count": 0})
                            for d in self.plan.decisions
                        ]
                    }
                )
            decisions: list[RenderDecision] = []
            for d in self.plan.decisions:
                if (
                    decisions
                    and (
                        d.action == decisions[-1].action == "DROP"
                        or any(
                            o.action == "SET_SPEED"
                            and o.source_id == d.source_id
                            and o.start_us <= decisions[-1].source_start_us
                            and o.end_us >= d.source_end_us
                            for o in project.overrides
                        )
                    )
                    and d.action not in ("HOLD", "MONTAGE")
                    and decisions[-1].action not in ("HOLD", "MONTAGE")
                    and d.crop == decisions[-1].crop
                    and d.subject_camera == decisions[-1].subject_camera
                    and d.source_id == decisions[-1].source_id
                    and d.source_start_us == decisions[-1].source_end_us
                ):
                    decisions[-1] = decisions[-1].model_copy(
                        update={
                            "source_end_us": d.source_end_us,
                            "output_frame_count": decisions[-1].output_frame_count
                            + d.output_frame_count,
                        }
                    )
                else:
                    decisions.append(d)
            for i, d in enumerate(decisions):
                state = tr("Пропуск") if d.action == "DROP" else tr("В ролике")
                item = QListWidgetItem(
                    f"{i + 1}. {state}\n"
                    f"{format_time(d.source_start_us / 1e6)}–"
                    f"{format_time(d.source_end_us / 1e6)}"
                )
                item.setData(Qt.ItemDataRole.UserRole, d)
                item.setSizeHint(QSize(132, 42))
                item.setToolTip(tr(f"{d.source_id}: время в исходнике. Нажмите для просмотра."))
                item.setForeground(QColor("#8891a0" if d.action == "DROP" else "#bcd1ff"))
                self.addItem(item)
            self.setVisible(bool(decisions))
            self.loaded.emit()
        except (ValueError, OSError) as exc:
            self.failed.emit(str(exc))

    def choose(self, item: QListWidgetItem) -> None:
        if self.plan is None or self.root is None:
            return
        menu = QMenu(self)
        decision: RenderDecision = item.data(Qt.ItemDataRole.UserRole)
        if decision.action in ("HOLD", "MONTAGE") or decision.sample_times_us:
            menu.addAction(tr("Стоп-кадр: границы заданы монтажом")).setEnabled(False)
            menu.aboutToHide.connect(menu.deleteLater)
            menu.popup(QCursor.pos())
            return
        for label, choice in (
            (tr("Оставить"), "keep"),
            (tr("Удалить"), "drop"),
            (tr("Показать медленнее"), "slow"),
        ):
            action = menu.addAction(label)
            action.triggered.connect(
                lambda checked=False, choice=choice: self.apply_choice(item, choice)
            )
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(QCursor.pos())

    def apply_choice(self, item: QListWidgetItem, choice: str) -> None:
        if self.plan is None or self.root is None or not self.isEnabled():
            return
        d: RenderDecision = item.data(Qt.ItemDataRole.UserRole)
        speed = (
            (
                (d.source_end_us - d.source_start_us)
                * self.plan.output_fps
                / (1_000_000 * d.output_frame_count)
            )
            if d.output_frame_count
            else 8.0
        )
        speed = max(1.0, speed / 2 if choice == "slow" else speed)
        try:
            before = load_project(self.root)
            project = edit_episode(before, d, None if choice == "drop" else speed)
            self.save_edit(
                before,
                project,
                tr("Эпизод: удаление") if choice == "drop" else tr("Эпизод: скорость"),
            )
            state = (
                tr("Удалён")
                if choice == "drop"
                else tr("Медленнее")
                if choice == "slow"
                else tr("Оставлен")
            )
            item.setText(state + "\n" + item.text().split("\n")[-1])
            item.setIcon(ui_icon("check"))
            # Repeated slowing operates on the latest selected speed.
            item.setData(
                Qt.ItemDataRole.UserRole,
                d.model_copy(
                    update={
                        "output_frame_count": 0
                        if choice == "drop"
                        else max(
                            1,
                            round(
                                (d.source_end_us - d.source_start_us)
                                * self.plan.output_fps
                                / 1_000_000
                                / speed
                            ),
                        ),
                    }
                ),
            )
            item.setForeground(QColor("#8891a0" if choice == "drop" else "#bcd1ff"))
            self.changed.emit(tr("Правка сохранена. Нажмите «Создать ролик», чтобы применить её."))
        except (ValueError, OSError) as exc:
            self.failed.emit(str(exc))

    def save_edit(self, before: Project, after: Project, label: str) -> None:
        if self.root is None:
            raise ValueError("Project is not open")
        # Validate before committing: never save an all-dropped/invalid plan.
        if self.validate_edits and self.plan is not None:
            checked = (after.model_copy(update={"settings": self.context_settings})
                       if self.context_settings is not None else after)
            apply_overrides(checked, self.plan)
        save_project(self.root, after)
        self.edit_saved.emit(before, after, label)
