"""A small reusable-settings library anchored to the main toolbar."""

from collections.abc import Callable

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sculptcut.compact_label import CompactLabel
from sculptcut.i18n import tr
from sculptcut.settings_templates import (
    SettingsTemplate,
    TemplateLibrary,
    TemplateOptions,
    preset_title,
    read_templates,
)
from sculptcut.time_format import format_time
from sculptcut.ui_icons import ui_icon
from sculptcut.ui_theme import apply_dialog_theme, style_message_box

TEMPLATES_KEY = "settings_templates"


class TemplateDialog(QDialog):
    def __init__(self, settings: QSettings, capture: Callable[[], TemplateOptions],
                 parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Шаблоны настроек"))
        self.setFixedWidth(340)
        apply_dialog_theme(self)
        self.settings, self.capture = settings, capture
        self.chosen: SettingsTemplate | None = None
        self.library = TemplateLibrary()
        self.readable = True
        self.creating = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(4)
        selection_row = QHBoxLayout()
        selection_row.setSpacing(4)
        self.items = QComboBox()
        self.items.setProperty("controlRole", "menu")
        self.items.setAccessibleName(tr("Сохранённые шаблоны"))
        self.items.setPlaceholderText(tr("Нет шаблонов"))
        self.items.setMinimumContentsLength(1)
        self.items.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.items.setMaxVisibleItems(8)
        self.items.currentIndexChanged.connect(self.selected)
        selection_row.addWidget(self.items, 1)
        self.name = QLineEdit()
        self.name.setMaxLength(80)
        self.name.setPlaceholderText(tr("Название шаблона"))
        self.name.setAccessibleName(tr("Название шаблона"))
        selection_row.addWidget(self.name, 1)
        self.name.hide()
        self.name.returnPressed.connect(self.save_current)
        self.save_button = QPushButton()
        self.save_button.setIcon(ui_icon("add"))
        self.save_button.setAccessibleName(tr("Создать шаблон"))
        self.save_button.setToolTip(tr("Создать шаблон"))
        self.save_button.clicked.connect(self.start_create)
        selection_row.addWidget(self.save_button)
        layout.addLayout(selection_row)
        self.message = CompactLabel()
        self.message.setFixedHeight(16)
        self.message.setVisible(False)
        self.delete_button = QPushButton()
        self.delete_button.setIcon(ui_icon("trash"))
        self.delete_button.setAccessibleName(tr("Удалить шаблон"))
        self.delete_button.setToolTip(tr("Удалить выбранный шаблон"))
        self.delete_button.setFixedSize(30, 30)
        self.delete_button.clicked.connect(self.delete_selected)
        self.apply_button = QPushButton()
        self.apply_button.setIcon(ui_icon("check"))
        self.apply_button.setAccessibleName(tr("Применить"))
        self.apply_button.setToolTip(tr("Применить настройки к текущему проекту (Ctrl+Z — отмена)"))
        self.apply_button.setObjectName("primary")
        self.apply_button.clicked.connect(self.confirm_selection)
        selection_row.addWidget(self.apply_button)
        selection_row.addWidget(self.delete_button)
        for control in (self.items, self.name, self.save_button, self.apply_button):
            control.setFixedHeight(30)
        for button in (self.save_button, self.apply_button, self.delete_button):
            button.setAutoDefault(False)
            button.setFixedSize(30, 30)
            button.setStyleSheet("QPushButton { padding:0; }")
        layout.addWidget(self.message)
        try:
            self.library = read_templates(str(settings.value(TEMPLATES_KEY, "")))
        except ValueError as exc:
            self.readable = False
            self.set_message(str(exc))
        self.save_button.setEnabled(self.readable)
        self.refresh()

    def set_message(self, message: str) -> None:
        self.message.setText(message)
        self.message.setVisible(bool(message))
        self.adjustSize()

    def refresh(self, selected_id: str = "") -> None:
        self.items.clear()
        row = 0
        for index, template in enumerate(self.library.templates):
            detail = (f"{preset_title(template.options.preset)} · "
                      f"{format_time(template.options.target_seconds)}")
            self.items.addItem(template.name)
            self.items.setItemData(index, template.name + "\n" + detail,
                                   Qt.ItemDataRole.ToolTipRole)
            if template.id == selected_id:
                row = index
        self.items.setCurrentIndex(row if self.library.templates else -1)
        self.selected(self.items.currentIndex())

    def selected(self, row: int) -> None:
        valid = 0 <= row < len(self.library.templates)
        self.apply_button.setEnabled((valid or self.creating) and self.readable)
        self.delete_button.setEnabled(valid and self.readable and not self.creating)
        if valid:
            self.items.setToolTip(self.items.itemData(row, Qt.ItemDataRole.ToolTipRole))
        else:
            self.items.setToolTip("")

    def start_create(self) -> None:
        if not self.readable:
            return
        if self.creating:
            self.finish_create()
            return
        self.creating = True
        self.save_button.setIcon(ui_icon("close"))
        self.save_button.setAccessibleName(tr("Отмена"))
        self.save_button.setToolTip(tr("Отмена"))
        self.items.hide()
        self.name.show()
        self.name.setFocus()
        self.apply_button.setAccessibleName(tr("Сохранить"))
        self.apply_button.setToolTip(tr("Сохранить"))
        self.set_message("")
        self.selected(self.items.currentIndex())

    def confirm_selection(self) -> None:
        if self.creating:
            self.save_current()
        else:
            self.apply_selected()

    def finish_create(self) -> None:
        self.creating = False
        self.save_button.setIcon(ui_icon("add"))
        self.save_button.setAccessibleName(tr("Создать шаблон"))
        self.save_button.setToolTip(tr("Создать шаблон"))
        self.name.clear()
        self.name.hide()
        self.items.show()
        self.apply_button.setAccessibleName(tr("Применить"))
        self.apply_button.setToolTip(
            tr("Применить настройки к текущему проекту (Ctrl+Z — отмена)"))
        self.selected(self.items.currentIndex())
        self.set_message("")
        self.items.setFocus()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self.creating and event.key() == Qt.Key.Key_Escape:
            self.finish_create()
            event.accept()
        else:
            super().keyPressEvent(event)

    def confirm(self, title: str, name: str, action: str) -> bool:
        dialog = QMessageBox(QMessageBox.Icon.Question, title, name,
                             QMessageBox.StandardButton.Cancel, self)
        button = dialog.addButton(action, QMessageBox.ButtonRole.AcceptRole)
        dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
        style_message_box(dialog)
        dialog.exec()
        confirmed = dialog.clickedButton() is button
        dialog.deleteLater()
        return confirmed

    def store(self, templates: list[SettingsTemplate], selected_id: str = "") -> None:
        candidate = TemplateLibrary(templates=templates)
        previous = self.settings.value(TEMPLATES_KEY, "")
        self.settings.setValue(TEMPLATES_KEY, candidate.model_dump_json())
        self.settings.sync()
        if self.settings.status() != QSettings.Status.NoError:
            self.settings.setValue(TEMPLATES_KEY, previous)
            self.settings.sync()
            raise OSError(tr("Не удалось сохранить шаблоны. Проверьте доступ к диску."))
        self.library = candidate
        self.refresh(selected_id)

    def save_current(self) -> None:
        if not self.readable:
            return
        try:
            name = SettingsTemplate.valid_name(self.name.text())
            template = SettingsTemplate(name=name, options=self.capture())
            existing = next((t for t in self.library.templates
                             if t.name.casefold() == template.name.casefold()), None)
            if existing is not None:
                if not self.confirm(tr("Заменить шаблон?"), existing.name, tr("Заменить")):
                    return
                template = template.model_copy(update={"id": existing.id})
            values = [template if existing is not None and t.id == existing.id else t
                      for t in self.library.templates]
            if existing is None:
                values.append(template)
            self.store(values, template.id)
            self.finish_create()
        except (ValueError, OSError) as exc:
            self.set_message(str(exc))

    def delete_selected(self) -> None:
        row = self.items.currentIndex()
        if not self.readable or not 0 <= row < len(self.library.templates):
            return
        template = self.library.templates[row]
        if not self.confirm(tr("Удалить шаблон?"), template.name, tr("Удалить")):
            return
        try:
            self.store([t for t in self.library.templates if t.id != template.id])
            if not self.library.templates:
                self.name.clear()
            self.set_message("")
        except (ValueError, OSError) as exc:
            self.set_message(str(exc))

    def apply_selected(self) -> None:
        row = self.items.currentIndex()
        if self.readable and 0 <= row < len(self.library.templates):
            self.chosen = self.library.templates[row]
            self.accept()
