"""Shared desktop theme and compact dialog elements, independent of MainWindow."""

from pathlib import Path

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sculptcut.i18n import tr
from sculptcut.ui_icons import IconName, ui_icon

STYLE = """
QWidget { background:#101318; color:#e9edf5; font:13px 'Segoe UI'; }
QMainWindow { background:#101318; }
QLabel { background:transparent; }
QLabel#title { color:#f7f9fc; font-size:22px; font-weight:700; }
QLabel#subtitle { color:#97a3b4; }
QLabel#eyebrow { color:#8291a4; font-size:11px; font-weight:650; }
QLabel#sectionTitle { color:#f2f5fa; font-size:15px; font-weight:650; }
QLabel#sectionHint { color:#9aa6b6; font-size:12px; }
QLabel#presetHint { color:#9aa8bb; font-size:12px; padding:2px 0 4px 0; }
QLabel#contextBadge,QLabel#privacyBadge {
 background:#1e2b3b; color:#a9c9ee; border:1px solid #34485f; border-radius:9px;
 padding:3px 8px; font-size:11px; font-weight:650;
}
QWidget#sourcesPanel,QWidget#settingsPanel,QWidget#viewerPanel {
 background:#181e26; border:0; border-radius:10px;
}
QWidget#modeSettingsPanel,QWidget#areaStack,QWidget#areaPage,
QWidget#musicPickerRow,QWidget#playbackVolume {
 background:transparent; border:0;
}
QLabel#modeSettingsTitle { color:#b6d3ff; font-size:12px; font-weight:650; }
QFrame#settingsDivider { background:#334757; border:0; }
QLabel#settingsCategory { color:#b4cadc; font-size:12px; font-weight:600; }
QLabel#settingsSubLabel { color:#91a3b5; font-size:11px; }
QWidget#exportBar { background:#181e26; border:0; border-radius:10px; }
QWidget#resultActionsBar { background:#171e27; border:0; border-radius:9px; }
QWidget#versionsPanel { background:#151b23; border:0; border-radius:9px; }
QFrame#videoDropZone { background:#0b1017; border:0; border-radius:10px; }
QFrame#videoDropZone QLabel#dropTitle { font-size:17px; }
QFrame#videoDropZone QLabel#dropHint { font-size:11px; }
QLabel#dropIcon { color:#6ea7f9; font-size:42px; font-weight:300; }
QLabel#dropTitle { color:#f0f5fd; font-size:20px; font-weight:650; }
QLabel#dropHint { color:#97a7bc; font-size:12px; }
QPushButton#dropChoose { color:#d7e8ff; border-color:#5798eb; background:#1c3556; }
QLabel#videoCanvas {
 background:#0b0f14; color:#a4b0be; border:0; border-radius:8px;
 padding:16px;
}
QLabel#introPreview { background:#0b0f14; border:0; border-radius:6px; }
QLineEdit,QSpinBox,QDoubleSpinBox,QListWidget,QTextEdit,QPlainTextEdit {
 background:#202832; border:1px solid #3b4959; border-radius:6px; padding:6px;
 selection-background-color:#4267a5; }
QLineEdit:focus,QSpinBox:focus,QDoubleSpinBox:focus,QTextEdit:focus,QPlainTextEdit:focus {
 border:1px solid #82acff; }
QComboBox {
 background:#111620; color:#e9edf5; border:1px solid #44516a; border-radius:7px;
 padding:7px 42px 7px 10px; selection-background-color:#4267a5;
}
QComboBox:hover { border-color:#657694; background:#161d29; }
QComboBox:focus { border:1px solid #82acff; }
QComboBox:disabled { color:#7f8999; border-color:#2c3442; }
QComboBox::drop-down {
 subcontrol-origin:padding; subcontrol-position:top right; width:34px;
 background:#273247; border-left:1px solid #44516a;
 border-top-right-radius:6px; border-bottom-right-radius:6px;
}
QComboBox::down-arrow { image:url("__CHEVRON__"); width:12px; height:8px; }
QComboBox#versionsChoice::drop-down {
 subcontrol-origin:border; width:26px; margin:5px 6px;
 border:1px solid #44516a; border-radius:4px;
}
QComboBox QAbstractItemView {
 background:#171d28; border:1px solid #52617a; outline:0; padding:4px;
 selection-background-color:#365889; selection-color:#fff;
}
QMenu { background:#1b2230; border:1px solid #43516a; padding:5px; }
QMenu::item { padding:8px 14px; border-radius:5px; }
QMenu::item:selected { background:#314b77; color:#f5f8ff; }
QMenu::item:disabled { color:#7f8999; }
QListWidget { border:0; padding:4px; outline:0; }
QListWidget::item { border-radius:6px; padding:7px 9px; margin:2px 0; }
QListWidget::item:hover { background:#252d3b; }
QListWidget::item:selected { background:#314b77; color:#f5f8ff; }
QPushButton { background:#26313d; color:#edf2f8; border:1px solid #455568;
 border-radius:6px; padding:8px 12px; font-weight:550; }
QPushButton:hover { background:#344456; border-color:#718ba6; }
QPushButton:pressed { background:#1b2434; }
QPushButton:focus { border:1px solid #9bbeff; }
QPushButton:disabled { color:#8891a0; background:#20252e; border-color:#2b303a; }
QWidget#cutSection {
 background:#14282b; border:1px solid #376369; border-radius:8px;
}
QWidget#outputCutPanel { background:transparent; border:0; }
QPushButton[actionRole="disclosure"] {
 background:transparent; border:0; border-radius:7px; padding:8px 10px;
 text-align:left; color:#b3ddd3; font-weight:600;
}
QPushButton[actionRole="disclosure"]:hover { background:#203e40; color:#e3f5ef; }
QPushButton[actionRole="disclosure"]:checked {
 background:#1c3a3c; color:#e3f5ef; border-bottom:1px solid #376369;
 border-bottom-left-radius:0; border-bottom-right-radius:0;
}
QPushButton[actionRole="disclosure"]:focus { border:1px solid #8ed5ca; }
QPushButton[actionRole="disclosure"]:disabled { background:#182b2d; color:#6e9290; }
QWidget#outputCutPanel QLabel#cutSummary { color:#9fbbb4; font-size:11px; }
QWidget#outputCutPanel QPushButton[actionRole="secondary"] {
 background:#1e3e3a; border-color:#466f69; color:#d6eae5; padding:6px 10px;
}
QWidget#outputCutPanel QPushButton[actionRole="secondary"]:hover { background:#2b514a; }
QWidget#outputCutPanel QPushButton[actionRole="secondary"]:disabled {
 background:#1a302d; border-color:#2d4843; color:#74968d;
}
QWidget#outputCutPanel QPushButton[actionRole="utility"] {
 background:transparent; border-color:transparent; color:#a4c4bb; padding:6px 8px;
}
QWidget#outputCutPanel QPushButton[actionRole="utility"]:hover {
 background:#264441; border-color:#3c6a63;
}
QWidget#outputCutPanel QPushButton[actionRole="utility"]:disabled { color:#607e75; }
QWidget#outputCutPanel QPushButton#primary {
 background:#327e73; border-color:#65b0a1; color:#f4fffa; padding:6px 12px;
}
QWidget#outputCutPanel QPushButton#primary:hover { background:#3c9185; }
QWidget#outputCutPanel QPushButton#primary:pressed { background:#28665e; }
QWidget#outputCutPanel QPushButton#primary:disabled {
 background:#203b39; border-color:#31504b; color:#76958e;
}
QWidget#outputCutPanel QPushButton:focus { border:1px solid #b6e4d7; }
QPushButton#musicFileButton {
 background:#202832; border:1px solid #3b4959; padding:0;
}
QPushButton#musicFileButton:hover { background:#2b3644; border-color:#718ba6; }
QPushButton#musicFileButton:pressed { background:#1b2434; }
QPushButton#musicFileButton:focus { border-color:#9bbeff; }
QPushButton#musicFileButton:disabled { background:#20252e; border-color:#2b303a; }
QPushButton#languageButton { padding:0; font-size:10px; color:#a9bbd2; }
QPushButton#languageButton:checked {
 background:#213f60; color:#f2f8ff; border:1px solid #76b4f4;
}
QPushButton#primary { background:#5d84d3; color:#fff; border-color:#8cacf0; font-weight:650; }
QPushButton#primary:hover { background:#7196e1; }
QPushButton#primary:disabled { background:#303d54; color:#9aa7be; }
QPushButton#durationChoice {
 background:#1b2531; border:1px solid #405268; color:#c8d5e5;
}
QPushButton#durationChoice:checked {
 background:#1e3a61; border:1px solid #64a4fa; color:#ecf5ff;
}
QPushButton#modeButton {
 background:#181e26; border:0; margin:0 0 6px 0;
 border-top-left-radius:9px; border-top-right-radius:9px;
 border-bottom-left-radius:4px; border-bottom-right-radius:4px;
 color:#b4becb; padding:3px 6px; font:600 18px 'Segoe UI';
}
QPushButton#modeButton:checked,
QPushButton#modeButton:checked:hover,
QPushButton#modeButton:checked:focus {
 background:#181e26; border:0; margin:0;
 border-bottom-left-radius:0; border-bottom-right-radius:0; color:#72b5ff;
}
QWidget#modeTabs { background:#0e141c; border:0; }
QPushButton#modeButton:hover { color:#ffffff; }
QPushButton#modeButton:focus { color:#b6d3ff; }
QPushButton[actionRole="secondary"] {
 background:#222b3a; border:1px solid #52627c; color:#edf2fa;
}
QPushButton[actionRole="secondary"]:checked,
QPushButton[actionRole="secondary"]:checked:hover {
 background:#1e3a61; border:1px solid #64a4fa; color:#ecf5ff;
}
QPushButton[actionRole="secondary"]:hover { background:#314058; border-color:#7891ba; }
QPushButton[actionRole="utility"] {
 background:transparent; border:1px solid #384457; color:#bdc8d8;
}
QPushButton[actionRole="utility"]:hover { background:#242d3c; color:#f1f5fb; }
QDialog,QMessageBox { background:#101318; }
QMessageBox QLabel { color:#e9edf5; }
QMessageBox QPushButton#destructiveButton { background:#9b5058; border-color:#c4747c; color:#fff; }
QMessageBox QPushButton#destructiveButton:hover { background:#b9616a; }
QMessageBox QPushButton#destructiveButton:pressed { background:#7d3f46; }
QProgressBar { background:#171b24; border:1px solid #343d4f; border-radius:6px;
 text-align:center; min-height:12px; }
QProgressBar::chunk { background:#6b94df; border-radius:4px; }
QScrollArea { border:0; background:transparent; }
QScrollBar:vertical { background:transparent; width:8px; margin:2px; }
QScrollBar::handle:vertical { background:#3a465a; min-height:28px; border-radius:4px; }
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical { height:0; }
QSplitter::handle { background:#0f1218; }
"""

STYLE += """
QWidget#dialogHeader { background:transparent; }
QLabel#dialogHeading { color:#f2f5fa; font-size:19px; font-weight:650; }
QLabel#dialogEmblem { background:#1e2b3b; border:0; border-radius:10px; }
QFrame#dialogPanel { background:#181e26; border:0; border-radius:10px; }
QCheckBox { background:transparent; spacing:9px; }
QCheckBox::indicator { width:17px; height:17px; background:#101720;
 border:1px solid #607089; border-radius:4px; }
QCheckBox::indicator:checked { background:#5d84d3; border:2px solid #b0d4ff; }
QCheckBox::indicator:hover { border-color:#b0d4ff; }
QCheckBox::indicator:disabled { background:#1e2630; border-color:#485465; }
QCheckBox::indicator:checked:disabled { background:#364255; border-color:#485465; }
QCheckBox:disabled { color:#788494; }
QWidget#playbackVolume QSlider { background:transparent; }
QSlider::groove:horizontal { height:6px; background:#293545; border-radius:3px; }
QSlider::sub-page:horizontal { background:#648fd8; border-radius:3px; }
QSlider::handle:horizontal { width:14px; margin:-5px 0; background:#a9cbff;
 border:1px solid #c4dcff; border-radius:7px; }
QSlider::handle:horizontal:hover { background:#d2e6ff; }
QSlider:disabled { background:transparent; }
QSlider::handle:horizontal:disabled { background:#485465; border-color:#607089; }
QScrollBar:horizontal { background:transparent; height:8px; margin:2px; }
QScrollBar::handle:horizontal { background:#3a465a; min-width:28px; border-radius:4px; }
QScrollBar::add-line:horizontal,QScrollBar::sub-line:horizontal { width:0; }
QToolTip { background:#202b3a; color:#e9edf5; border:1px solid #52627c; padding:6px; }
"""
STYLE = STYLE.replace("__CHEVRON__", (Path(__file__).parent / "assets/chevron-down.svg").as_posix())


def apply_dialog_theme(dialog: QDialog, extra: str = "") -> None:
    """Theme dialogs even when opened without the main window as their parent."""
    dialog.setStyleSheet(STYLE + extra)


def dialog_header(title: str, icon: IconName, accent: str = "#a9cbff") -> QWidget:
    header = QWidget()
    header.setObjectName("dialogHeader")
    row = QHBoxLayout(header)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(12)
    emblem = QLabel()
    emblem.setObjectName("dialogEmblem")
    emblem.setFixedSize(44, 44)
    emblem.setAlignment(Qt.AlignmentFlag.AlignCenter)
    emblem.setPixmap(ui_icon(icon, accent).pixmap(26, 26))
    row.addWidget(emblem)
    label = QLabel(title)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setObjectName("dialogHeading")
    row.addWidget(label, 1)
    return header


def style_setup_dialog(dialog: QInputDialog, title: str) -> None:
    apply_dialog_theme(dialog)
    layout = dialog.layout()
    if isinstance(layout, QVBoxLayout):
        layout.setContentsMargins(20, 20, 20, 18)
        layout.setSpacing(12)
        layout.insertWidget(0, dialog_header(title, "feedback"))


def style_dialog_buttons(box: QDialogButtonBox, accept_text: str = "Применить") -> None:
    accept = box.button(QDialogButtonBox.StandardButton.Ok)
    if accept is not None:
        accept.setText(tr(accept_text))
        accept.setObjectName("primary")
        accept.setMinimumWidth(116)
        accept.style().unpolish(accept)
        accept.style().polish(accept)


class DialogButtons(QDialogButtonBox):
    """Keep the action label/theme when Qt retranslates standard buttons on show."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, parent,
        )
        style_dialog_buttons(self)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.LanguageChange, QEvent.Type.StyleChange):
            style_dialog_buttons(self)


def style_message_box(box: QMessageBox) -> None:
    """Preserve Qt roles/defaults/escape behavior while removing native chrome."""
    box.setOption(QMessageBox.Option.DontUseNativeDialog, True)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setIconPixmap(ui_icon("info", "#efc681").pixmap(32, 32))
    layout = box.layout()
    if layout is not None:
        layout.setContentsMargins(20, 20, 20, 18)
        layout.setSpacing(14)
    for button in box.buttons():
        button.setMinimumHeight(36)
        role = box.buttonRole(button)
        if role == QMessageBox.ButtonRole.DestructiveRole:
            button.setObjectName("destructiveButton")
        elif role in (QMessageBox.ButtonRole.AcceptRole, QMessageBox.ButtonRole.YesRole):
            button.setObjectName("primary")
            if isinstance(button, QPushButton) and box.defaultButton() is None:
                box.setDefaultButton(button)
            if box.escapeButton() is None:
                box.setEscapeButton(button)
    apply_dialog_theme(box)


def show_notice(parent: QWidget | None, title: str, message: str) -> None:
    box = QMessageBox(QMessageBox.Icon.Information, title, message,
                      QMessageBox.StandardButton.Ok, parent)
    style_message_box(box)
    box.exec()
    box.deleteLater()
