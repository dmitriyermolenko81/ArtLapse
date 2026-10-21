"""One shared presentation for a project, chosen from its existing sources."""

from collections.abc import Mapping
from pathlib import Path
from typing import Literal, cast

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sculptcut.i18n import tr
from sculptcut.ui_icons import ui_icon
from sculptcut.ui_theme import apply_dialog_theme, dialog_header


class PlacementSelector(QWidget):
    """Two visible choices with the value API of the former combo box."""

    def __init__(self, position: str) -> None:
        super().__init__()
        self.group = QButtonGroup(self)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        for index, caption in enumerate((tr("В начале"), tr("В конце"))):
            button = QToolButton()
            button.setText(caption)
            button.setIcon(ui_icon("previous" if index == 0 else "next"))
            button.setIconSize(QSize(20, 20))
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setCheckable(True)
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            button.setAccessibleName(caption + tr(" ролика"))
            button.setMinimumHeight(46)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self.group.addButton(button, index)
            row.addWidget(button, 1)
        self.setCurrentIndex(max(0, self.findData(position)))

    def findData(self, value: str) -> int:
        return {"start": 0, "end": 1}.get(value, -1)

    def setCurrentIndex(self, index: int) -> None:
        button = self.group.button(index)
        if button is not None:
            button.setChecked(True)

    def currentData(self) -> str:
        return "end" if self.group.checkedId() == 1 else "start"


class PresentationDialog(QDialog):
    def __init__(self, video: Path | None, seconds: float, position: str,
                 sources: list[Path], parent: QWidget | None = None, *,
                 media: Mapping[str, tuple[QImage, str]] | None = None) -> None:
        super().__init__(parent)
        self._media = dict(media or {})
        self.setWindowTitle(tr("Презентация проекта"))
        self.setFixedWidth(510)
        apply_dialog_theme(self, """
            QFrame#presentationSource { background:#181e26; border:0; border-radius:10px; }
            QLabel#presentationThumbnail { background:#0b1017; border-radius:6px; }
            QLabel#presentationCaption { color:#bac8da; font-size:12px; }
            QToolButton { background:#202936; border:1px solid #3b495e;
                border-radius:8px; color:#bac8da; padding:0 8px; }
            QToolButton:hover { background:#2a384a; border-color:#728fb8; }
            QToolButton:checked { background:#213f60; border:2px solid #76b4f4;
                color:#f2f8ff; padding:0 7px; }
            QToolButton:focus { border-color:#b8d8ff; }
            QToolButton:disabled { color:#657185; background:#1b222c; border-color:#303a49; }
            QDoubleSpinBox { font-size:21px; font-weight:600; padding:0 8px; }
            QDoubleSpinBox:disabled { color:#657185; background:#1b222c; border-color:#303a49; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 18)
        layout.setSpacing(16)
        layout.addWidget(dialog_header(self.windowTitle(), "presentation", "#efc681"))

        source = QFrame()
        source.setObjectName("presentationSource")
        source_layout = QHBoxLayout(source)
        source_layout.setContentsMargins(12, 12, 12, 12)
        source_layout.setSpacing(12)
        self.thumbnail = QLabel()
        self.thumbnail.setObjectName("presentationThumbnail")
        self.thumbnail.setFixedSize(96, 54)
        self.thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        source_layout.addWidget(self.thumbnail)
        self.video = QComboBox()
        self.video.setProperty("controlRole", "menu")
        self.video.setMinimumContentsLength(16)
        self.video.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.video.addItem(tr("Без презентации"), "")
        for path in dict.fromkeys(p.resolve() for p in sources):
            self.video.addItem(path.name, str(path))
            self.video.setItemData(self.video.count() - 1, str(path), Qt.ItemDataRole.ToolTipRole)
        selected = max(0, self.video.findData(str(video.resolve()))) if video else 0
        self.video.setCurrentIndex(selected)
        self.video.setAccessibleName(tr("Презентационный ролик проекта"))
        self.video.setToolTip(tr("Выберите один из добавленных исходников"))
        source_layout.addWidget(self.video, 1)
        layout.addWidget(source)

        timing = QHBoxLayout()
        timing.setSpacing(16)
        placement_column = QVBoxLayout()
        placement_column.setSpacing(7)
        placement_label = QLabel(tr("Где показать"))
        placement_label.setObjectName("presentationCaption")
        placement_column.addWidget(placement_label)
        self.position = PlacementSelector(position)
        placement_column.addWidget(self.position)
        timing.addLayout(placement_column, 1)
        duration_column = QVBoxLayout()
        duration_column.setSpacing(7)
        duration_label = QLabel(tr("Длительность"))
        duration_label.setObjectName("presentationCaption")
        duration_column.addWidget(duration_label)
        self.seconds = QDoubleSpinBox()
        self.seconds.setRange(.5, 60)
        self.seconds.setDecimals(1)
        self.seconds.setSingleStep(.5)
        self.seconds.setSuffix(tr(" с"))
        self.seconds.setValue(seconds)
        self.seconds.setFixedSize(128, 46)
        self.seconds.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.seconds.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.seconds.setAccessibleName(tr("Длительность презентации"))
        self.seconds.setToolTip(tr("Весь клип за выбранное время: от 0,5 до 60 секунд.\n"
                                "Скорость изменится, исходный звук не используется."))
        duration_column.addWidget(self.seconds)
        timing.addLayout(duration_column)
        layout.addLayout(timing)
        self.note = QLabel()
        self.note.setStyleSheet("color:#f0ad9e; font-size:12px;")
        self.note.setWordWrap(True)
        self.note.hide()
        layout.addWidget(self.note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.apply_button = buttons.addButton(
            tr("Применить"), QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.apply_button.setObjectName("primary")
        self.apply_button.setMinimumWidth(116)
        self.apply_button.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.video.currentIndexChanged.connect(self.update_controls)
        self.update_controls()

    @property
    def path(self) -> Path | None:
        value = str(self.video.currentData() or "")
        return Path(value) if value else None

    @property
    def placement(self) -> Literal["start", "end"]:
        return cast(Literal["start", "end"], self.position.currentData())

    def set_source_media(self, path: str, frame: QImage, detail: str) -> None:
        """Reuse source-card results, including those arriving after the dialog opens."""
        self._media[path] = (frame, detail)
        if self.path == Path(path):
            self.update_preview()

    def update_preview(self) -> None:
        path = self.path
        self.thumbnail.setVisible(path is not None)
        self.thumbnail.setPixmap(ui_icon("presentation", "#8396af").pixmap(30, 30))
        if path is not None:
            cached = self._media.get(str(path))
            if cached is not None:
                frame, _ = cached
                if not frame.isNull():
                    self.thumbnail.setPixmap(QPixmap.fromImage(frame).scaled(
                        self.thumbnail.size(), Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    ))

    def update_controls(self) -> None:
        self.position.setEnabled(self.path is not None)
        self.seconds.setEnabled(self.path is not None)
        self.note.hide()
        self.update_preview()
        self.adjustSize()

    def accept(self) -> None:
        if self.path is not None and not self.path.is_file():
            self.note.setText(tr("Файл не найден. Найдите исходное видео "
                              "или выберите «Без презентации»."))
            self.note.show()
            return
        super().accept()
