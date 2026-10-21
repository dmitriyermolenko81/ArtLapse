"""Compact project cleanup choices and explicit confirmation of folder deletion."""

from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QListWidget,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sculptcut.i18n import tr
from sculptcut.project_cleanup import Category, CleanupFile, format_bytes
from sculptcut.project_deletion import ProjectFolder, project_deletion_target
from sculptcut.ui_theme import apply_dialog_theme, dialog_header, style_message_box


class CleanupDialog(QDialog):
    def __init__(
        self, root: Path, files: tuple[CleanupFile, ...], parent: QWidget, *,
        extra_inputs: Iterable[Path] = (),
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Очистить проект"))
        apply_dialog_theme(self, """
            QFrame#cleanupOptions { background:#181e26; border:0; border-radius:10px; }
            QCheckBox#deleteProject { color:#f0aab0; }
            QCheckBox#deleteProject:disabled { color:#788494; }
            QCheckBox#deleteProject::indicator:checked { background:#af4652;
                border-color:#f0b7be; }
            QPushButton#deleteSelection { background:#71333e; color:#fff0f2;
                border:1px solid #a85360; border-radius:6px; padding:8px 14px; }
            QPushButton#deleteSelection:hover { background:#8b3c4b; }
            QPushButton#deleteSelection:disabled { background:#232a34;
                color:#788494; border-color:#343e4c; }
        """)
        self.root = root
        self.files = files
        self.project_target: ProjectFolder | None = None
        self.folder_error = ""
        try:
            self.project_target = project_deletion_target(root, extra_inputs)
        except (OSError, ValueError) as exc:
            self.folder_error = tr(str(exc))
        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)
        layout.setContentsMargins(20, 20, 20, 18)
        layout.setSpacing(12)
        layout.addWidget(dialog_header(tr("Очистить проект"), "trash", "#f0aab0"))
        project = QLabel(self.fontMetrics().elidedText(root.name, Qt.TextElideMode.ElideRight, 420))
        project.setTextFormat(Qt.TextFormat.PlainText)
        project.setObjectName("sectionHint")
        project.setToolTip(str(root))
        layout.addWidget(project)
        panel = QFrame()
        panel.setObjectName("cleanupOptions")
        panel.setMinimumWidth(380)
        rows = QVBoxLayout(panel)
        rows.setContentsMargins(10, 8, 10, 10)
        rows.setSpacing(0)
        self.categories: dict[Category, QCheckBox] = {}
        options: tuple[tuple[Category, str, str], ...] = (
            ("renders", tr("Готовые ролики"), tr("Все созданные MP4 и их история версий")),
            ("previews", tr("Предпросмотры"), tr("Пробные ролики; можно собрать заново")),
            ("proxy", tr("Прокси анализа"), tr("Анализ заново подготовит эти видео")),
        )
        for category, text, tooltip in options:
            group = [f for f in files if f.category == category]
            count = sum(f.path == f.video for f in group)
            box = QCheckBox(text)
            box.setToolTip(tooltip)
            box.setEnabled(bool(group))
            box.toggled.connect(self.refresh)
            self.categories[category] = box
            row = QHBoxLayout()
            row.setContentsMargins(0, 7, 0, 7)
            row.addWidget(box)
            row.addStretch()
            stats = QLabel(f"{count} · {format_bytes(sum(f.size for f in group))}"
                           if group else tr("Пусто"))
            stats.setObjectName("subtitle")
            stats.setToolTip(tr(f"Видео: {count}"))
            row.addWidget(stats)
            rows.addLayout(row)
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setStyleSheet("color:#334155;")
        rows.addWidget(separator)
        self.delete_project = QCheckBox(tr("Удалить папку проекта целиком"))
        self.delete_project.setObjectName("deleteProject")
        self.delete_project.setToolTip(self.folder_error or str(root))
        self.delete_project.toggled.connect(self.refresh)
        rows.addWidget(self.delete_project)
        layout.addWidget(panel)
        summary_row = QHBoxLayout()
        self.summary = QLabel()
        summary_row.addWidget(self.summary, 1)
        self.details_button = QPushButton(tr("Файлы"))
        self.details_button.setCheckable(True)
        self.details_button.setFlat(True)
        self.details_button.setProperty("actionRole", "utility")
        self.details_button.setAutoDefault(False)
        self.details_button.toggled.connect(self.toggle_details)
        summary_row.addWidget(self.details_button)
        layout.addLayout(summary_row)
        self.file_list = QListWidget()
        self.file_list.setAccessibleName(tr("Файлы для удаления"))
        self.file_list.setFixedHeight(96)
        self.file_list.hide()
        layout.addWidget(self.file_list)
        self.warning = QLabel()
        self.warning.setWordWrap(True)
        self.warning.setObjectName("subtitle")
        self.warning.setToolTip(tr("Удаление безвозвратно. "
                                "Для всей папки нужно отдельное подтверждение."))
        layout.addWidget(self.warning)
        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = QPushButton(tr("Отмена"))
        cancel.setDefault(True)
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        self.remove_button = QPushButton(tr("Удалить"))
        self.remove_button.setObjectName("deleteSelection")
        self.remove_button.setAutoDefault(False)
        self.remove_button.clicked.connect(self.confirm_selection)
        actions.addWidget(self.remove_button)
        layout.addLayout(actions)
        self.refresh()

    def selected_files(self) -> tuple[CleanupFile, ...]:
        if self.delete_project.isChecked():
            return ()
        return tuple(f for f in self.files if self.categories[f.category].isChecked())

    def toggle_details(self, expanded: bool) -> None:
        visible = expanded and bool(self.selected_files())
        self.file_list.setVisible(visible)

    def refresh(self) -> None:
        whole = self.delete_project.isChecked()
        for category, box in self.categories.items():
            box.setEnabled(not whole and any(f.category == category for f in self.files))
        files = self.selected_files()
        self.file_list.clear()
        self.file_list.addItems([
            f"{f.path.relative_to(self.root)} · {format_bytes(f.size)}" for f in files
        ])
        self.summary.setText(tr("Проект будет закрыт") if whole else
                             tr(f"{len(files)} файлов · {format_bytes(sum(f.size for f in files))}")
                             if files else tr("Выберите, что удалить"))
        self.warning.setText(self.folder_error if whole and self.project_target is None else
                             tr("Удалится папка со всеми файлами и настройками.") if whole else
                             tr("Исходники и настройки проекта сохранятся."))
        self.details_button.setVisible(bool(files))
        self.toggle_details(self.details_button.isChecked())
        self.remove_button.setText(tr("Удалить проект") if whole else tr("Удалить"))
        self.remove_button.setEnabled((whole and self.project_target is not None) or bool(files))

    def confirm_project_deletion(self) -> bool:
        confirm = QMessageBox(self)
        confirm.setWindowTitle(tr("Удалить проект целиком?"))
        confirm.setIcon(QMessageBox.Icon.Warning)
        confirm.setTextFormat(Qt.TextFormat.PlainText)
        confirm.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        confirm.setText(tr("Будут удалены папка проекта и всё её содержимое."))
        confirm.setInformativeText(
            str(self.root.absolute()) + tr("\n\nЭто действие нельзя отменить.")
        )
        remove = confirm.addButton(tr("Удалить папку"), QMessageBox.ButtonRole.DestructiveRole)
        cancel = confirm.addButton(tr("Отмена"), QMessageBox.ButtonRole.RejectRole)
        confirm.setDefaultButton(cancel)
        confirm.setEscapeButton(cancel)
        style_message_box(confirm)
        confirm.exec()
        return confirm.clickedButton() is remove

    def confirm_selection(self) -> None:
        if self.delete_project.isChecked() and not self.confirm_project_deletion():
            return
        if self.remove_button.isEnabled():
            self.accept()
