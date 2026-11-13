"""Still-image bookend controls matching the project presentation dialog."""

from pathlib import Path
from typing import Literal, cast

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sculptcut.i18n import tr
from sculptcut.presentation_dialog import PlacementSelector
from sculptcut.ui_icons import ui_icon
from sculptcut.ui_theme import apply_dialog_theme, dialog_header


class IntroDialog(QDialog):
    def __init__(
        self, image: Path | None, seconds: float, position: str,
        parent: QWidget | None = None, *, opening_presentation: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Настройки заставки"))
        self.setFixedWidth(510)
        apply_dialog_theme(self, """
            QToolButton { background:#202936; border:1px solid #3b495e;
                border-radius:8px; color:#bac8da; padding:0 8px; }
            QToolButton:hover { background:#2a384a; border-color:#728fb8; }
            QToolButton:checked { background:#213f60; border:2px solid #76b4f4;
                color:#f2f8ff; padding:0 7px; }
            QToolButton:disabled { color:#657185; background:#1b222c; }
            QDoubleSpinBox { font-size:21px; font-weight:600; padding:0 8px; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 18)
        layout.setSpacing(16)
        layout.addWidget(dialog_header(self.windowTitle(), "image", "#efc681"))
        self.preview = QLabel()
        self.preview.setFixedSize(470, 140)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet("background:#0b1017; border-radius:8px;")
        layout.addWidget(self.preview)
        source = QHBoxLayout()
        self.image = QLineEdit(str(image) if image else "")
        self.image.setReadOnly(True)
        self.image.setPlaceholderText(tr("Без заставки"))
        self.image.setAccessibleName(tr("Изображение заставки"))
        source.addWidget(self.image, 1)
        self.browse_button = QPushButton(tr("Выбрать"))
        self.browse_button.clicked.connect(self.browse)
        source.addWidget(self.browse_button)
        self.clear_button = QPushButton()
        self.clear_button.setIcon(ui_icon("close"))
        self.clear_button.setFixedSize(36, 36)
        self.clear_button.setToolTip(tr("Убрать заставку"))
        self.clear_button.setAccessibleName(tr("Убрать заставку"))
        self.clear_button.clicked.connect(self.image.clear)
        source.addWidget(self.clear_button)
        layout.addLayout(source)
        timing = QHBoxLayout()
        placement = QVBoxLayout()
        placement.addWidget(QLabel(tr("Где показать")))
        self.position = PlacementSelector("end" if opening_presentation else position)
        opening = self.position.group.button(0)
        opening.setEnabled(not opening_presentation)
        hint = tr("В начале уже стоит презентация. Заставка будет в конце.")
        if opening_presentation:
            opening.setToolTip(hint)
        placement.addWidget(self.position)
        timing.addLayout(placement, 1)
        duration = QVBoxLayout()
        duration.addWidget(QLabel(tr("Длительность")))
        self.seconds = QDoubleSpinBox()
        self.seconds.setRange(.5, 60)
        self.seconds.setDecimals(1)
        self.seconds.setSingleStep(.5)
        self.seconds.setSuffix(tr(" с"))
        self.seconds.setValue(seconds)
        self.seconds.setFixedSize(128, 46)
        self.seconds.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.seconds.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.seconds.setAccessibleName(tr("Длительность заставки"))
        duration.addWidget(self.seconds)
        timing.addLayout(duration)
        layout.addLayout(timing)
        self.note = QLabel(hint if opening_presentation else "")
        self.note.setWordWrap(True)
        self.note.setVisible(opening_presentation)
        layout.addWidget(self.note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.apply_button = buttons.addButton(tr("Применить"),
                                              QDialogButtonBox.ButtonRole.AcceptRole)
        self.apply_button.setObjectName("primary")
        self.apply_button.setMinimumWidth(116)
        self.apply_button.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.image.textChanged.connect(self.update_preview)
        self.update_preview()

    @property
    def path(self) -> Path | None:
        return Path(self.image.text()) if self.image.text() else None

    @property
    def placement(self) -> Literal["start", "end"]:
        return cast(Literal["start", "end"], self.position.currentData())

    def browse(self) -> None:
        self.setProperty("nativeDialogOpen", True)
        try:
            path, _ = QFileDialog.getOpenFileName(
                self, tr("Выберите изображение заставки"), "",
                tr("Изображения (*.png *.jpg *.jpeg *.webp *.bmp)"),
            )
        finally:
            self.setProperty("nativeDialogOpen", False)
        if path:
            self.image.setText(str(Path(path).resolve()))

    def update_preview(self) -> None:
        image = QPixmap(self.image.text()) if self.path else QPixmap()
        self.preview.setVisible(self.path is not None)
        self.preview.setPixmap(image.scaled(
            self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ) if not image.isNull() else QPixmap())
        self.seconds.setEnabled(self.path is not None)
        self.position.setEnabled(self.path is not None)
        self.clear_button.setEnabled(self.path is not None)
        self.adjustSize()

    def accept(self) -> None:
        if self.path is not None and (not self.path.is_file()
                                      or QPixmap(str(self.path)).isNull()):
            self.note.setText(tr("Не удалось прочитать изображение заставки: ") + str(self.path))
            self.note.show()
            return
        super().accept()
