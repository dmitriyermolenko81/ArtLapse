"""Explicit choice between trimming an export and refilling its source edit."""

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from sculptcut.i18n import tr
from sculptcut.ui_theme import apply_dialog_theme, dialog_header


class CutSaveDialog(QDialog):
    def __init__(self, unavailable_reason: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Сохранение подрезки"))
        self.setMinimumWidth(500)
        apply_dialog_theme(self, """
QRadioButton { background:transparent; spacing:10px; font-weight:600; }
QRadioButton::indicator { width:17px; height:17px; border-radius:9px;
    background:#101720; border:1px solid #52627c; }
QRadioButton::indicator:checked { background:#5d84d3; border:3px solid #b0d4ff; }
QRadioButton:disabled { color:#788494; }
QRadioButton::indicator:disabled { background:#1e2630; border-color:#485465; }
""")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        layout.addWidget(dialog_header(tr("Как сохранить вырезки?"), "cut"))
        self.trim = QRadioButton(tr("Просто вырезать"))
        self.trim.setChecked(True)
        layout.addWidget(self.trim)
        self.trim_note = QLabel(tr("Ролик станет короче. Темп оставшегося монтажа не изменится."))
        self.trim_note.setObjectName("subtitle")
        self.trim_note.setWordWrap(True)
        layout.addWidget(self.trim_note)
        self.refill = QRadioButton(tr("Вырезать и сохранить длительность"))
        self.refill.setEnabled(not unavailable_reason)
        layout.addWidget(self.refill)
        self.refill_note = QLabel(tr(
            "Оставшиеся эпизоды будут растянуты до прежней длительности. "
            "Темп монтажа изменится."
        ))
        self.refill_note.setObjectName("subtitle")
        self.refill_note.setWordWrap(True)
        layout.addWidget(self.refill_note)
        self.reason = QLabel(unavailable_reason)
        self.reason.setWordWrap(True)
        self.reason.setObjectName("subtitle")
        self.reason.setVisible(bool(unavailable_reason))
        layout.addWidget(self.reason)
        self.refill.setToolTip(unavailable_reason)
        note = QLabel(tr("Результат сохранится в новый файл. Предыдущая версия останется."))
        note.setWordWrap(True)
        layout.addWidget(note)
        actions = QHBoxLayout()
        actions.addStretch()
        self.cancel_button = QPushButton(tr("Отмена"))
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(self.reject)
        actions.addWidget(self.cancel_button)
        self.save_button = QPushButton(tr("Сохранить копию"))
        self.save_button.setObjectName("primary")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self.accept)
        actions.addWidget(self.save_button)
        layout.addLayout(actions)

    @property
    def keep_duration(self) -> bool:
        return self.refill.isEnabled() and self.refill.isChecked()
