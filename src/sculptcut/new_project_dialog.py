"""Compact creation form; filesystem mutation runs separately in the job worker."""

from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sculptcut.i18n import tr
from sculptcut.project import new_project_path
from sculptcut.ui_icons import ui_icon
from sculptcut.ui_theme import apply_dialog_theme


class NewProjectDialog(QDialog):
    def __init__(self, parent_directory: Path, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Создать проект"))
        self.setFixedWidth(460)
        apply_dialog_theme(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 18)
        layout.setSpacing(12)
        layout.addWidget(QLabel(tr("Название проекта")))
        self.name = QLineEdit(tr("Новый проект"))
        self.name.setAccessibleName(tr("Название нового проекта"))
        self.name.setMaxLength(100)
        self.name.selectAll()
        layout.addWidget(self.name)
        layout.addWidget(QLabel(tr("Расположение")))
        location = QHBoxLayout()
        self.directory = QLineEdit(str(parent_directory))
        self.directory.setAccessibleName(tr("Папка для нового проекта"))
        location.addWidget(self.directory, 1)
        browse = QPushButton()
        browse.setIcon(ui_icon("folder"))
        browse.setFixedWidth(38)
        browse.setAccessibleName(tr("Выбрать папку для нового проекта"))
        browse.setToolTip(browse.accessibleName())
        browse.setAutoDefault(False)
        browse.clicked.connect(self.choose_directory)
        location.addWidget(browse)
        layout.addLayout(location)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color:#f0ad9e; font-size:12px;")
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, parent=self)
        self.create_button = buttons.addButton(
            tr("Создать проект"), QDialogButtonBox.ButtonRole.AcceptRole,
        )
        self.create_button.setObjectName("primary")
        self.create_button.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.name.textChanged.connect(self.refresh)
        self.directory.textChanged.connect(self.refresh)
        self.target: Path | None = None
        self.refresh()

    def choose_directory(self) -> None:
        # Native file selection temporarily owns input/focus outside the embedded panel.
        self.setProperty("nativeDialogOpen", True)
        try:
            chosen = QFileDialog.getExistingDirectory(self, tr("Где создать проект?"),
                                                       self.directory.text())
        finally:
            self.setProperty("nativeDialogOpen", False)
        if chosen:
            self.directory.setText(chosen)

    def refresh(self) -> None:
        try:
            if not self.directory.text().strip():
                raise ValueError(tr("Выберите папку для проекта."))
            self.target = new_project_path(Path(self.directory.text().strip()), self.name.text())
        except (OSError, ValueError) as error:
            self.target = None
            self.error.setText(tr(str(error)))
        self.error.setVisible(self.target is None)
        self.create_button.setEnabled(self.target is not None)

    def accept(self) -> None:
        self.refresh()
        if self.target is not None:
            super().accept()
