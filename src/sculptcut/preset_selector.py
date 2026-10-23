"""Single-row preset buttons with an exclusive selection and keyboard focus."""

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QButtonGroup, QGridLayout, QLabel, QSizePolicy, QToolButton, QWidget

from sculptcut.i18n import tr
from sculptcut.shorts_layout import SHORTS_PRESETS
from sculptcut.ui_icons import IconName, ui_icon

PRESENTATION: dict[str, tuple[str, IconName]] = {
    "timelapse": ("Эпизоды", "timelapse"),
    "speed": ("Ускорение", "speed"),
    "shorts": ("9:16", "portrait"),
    "music_shorts": ("Эпизоды", "episodes_music"),
    "beat_shorts": ("Музыка", "portrait_music"),
    "photoshop_shorts": ("Ps 9:16", "drawing"),
    "photoshop_music_shorts": ("Ps эпизоды", "episodes_music"),
    "photoshop_beat_shorts": ("Ps музыка", "drawing_music"),
    "auto": ("9:16", "layout_portrait"),
    "square": ("1:1 с полями", "layout_square"),
    "dynamic": ("Фрагменты", "music_fragments"),
    "full_tracks": ("Треки целиком", "music_tracks"),
    "full": ("Полное видео", "screen"),
    "viewport": ("Рамка", "crop"),
}

PRESET_DESCRIPTIONS = {
    "timelapse": "Убирает паузы и показывает основные этапы работы.",
    "shorts": "Вертикальный ролик из коротких эпизодов работы.",
    "music_shorts": "Движущиеся видеоэпизоды сменяются под музыкальные акценты.",
    "beat_shorts": "Стоп-кадры сменяются в ритме выбранной музыки.",
    "speed": "Вся запись ускоряется без удаления участков.",
    "photoshop_shorts": "Вертикальный монтаж с камерой внутри холста.",
    "photoshop_music_shorts": "Движущиеся эпизоды рисования сменяются под музыкальные акценты.",
    "photoshop_beat_shorts": "Стадии рисунка сменяются под музыку; рамка неподвижна.",
}

PRESET_ACCENTS = {
    "timelapse": ("#a9cbff", "#213f60", "#29394f"),
    "speed": ("#a9cbff", "#213f60", "#29394f"),
    "shorts": ("#79ded0", "#1d4949", "#263e42"),
    "music_shorts": ("#ffd28c", "#59422c", "#42392d"),
    "photoshop_shorts": ("#79ded0", "#1d4949", "#263e42"),
    "photoshop_music_shorts": ("#ffd28c", "#59422c", "#42392d"),
    "beat_shorts": ("#c7a5f5", "#44335e", "#353044"),
    "photoshop_beat_shorts": ("#c7a5f5", "#44335e", "#353044"),
}

ASPECT_CAPTIONS = {
    "auto": "9:16", "square": "1:1", "full": "16:9", "viewport": "16:9",
}


class PresetSelector(QWidget):
    currentIndexChanged = Signal(int)
    about_to_activate = Signal()
    activated = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setProperty("controlRole", "presetButtons")
        self._items: list[tuple[str, str]] = []
        self._index = -1
        self.buttons: dict[str, QToolButton] = {}
        self._group = QButtonGroup(self)
        self._group.idClicked.connect(self.activate)
        self._row = QGridLayout(self)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(6)
        self._slots: list[QWidget] = []
        self.setStyleSheet("""
            QToolButton { background:#202936; border:1px solid #3b495e;
                border-radius:9px; padding:6px 2px; color:#b4c2d7;
                font:11px 'Segoe UI'; }
            QToolButton:hover { background:#2a384a; border-color:#728fb8; }
            QToolButton:checked { background:#213f60; border:2px solid #76b4f4;
                color:#f2f8ff; padding:5px 1px; }
            QToolButton:focus { border-color:#b8d8ff; }
            QToolButton:disabled { color:#657185; background:#1b222c; border-color:#303a49; }
            QToolButton:checked:disabled { background:#213f60; border:2px solid #76b4f4;
                color:#d5e7fb; padding:5px 1px; }
        """)

    def addItem(self, title: str, value: str) -> None:
        caption, icon = PRESENTATION[value]
        caption = tr(caption)
        button = QToolButton(self)
        button.setText(ASPECT_CAPTIONS.get(value, caption))
        palette = PRESET_ACCENTS.get(value)
        button.setIcon(ui_icon(icon, palette[0]) if palette else ui_icon(icon))
        if palette:
            accent, selected, hover = palette
            button.setStyleSheet(f"""
                QToolButton:hover {{ background:{hover}; border-color:{accent}; }}
                QToolButton:checked {{ background:{selected}; border:2px solid {accent}; }}
                QToolButton:focus {{ border-color:{accent}; }}
                QToolButton:disabled {{ background:#1b222c; border-color:#303a49; }}
                QToolButton:checked:disabled {{ background:{selected};
                    border:2px solid {accent}; color:#d5e7fb; }}
            """)
        button.setIconSize(QSize(28, 28))
        button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextUnderIcon if value in ASPECT_CAPTIONS
            else Qt.ToolButtonStyle.ToolButtonIconOnly
        )
        if value in ASPECT_CAPTIONS:
            button.setStyleSheet(button.styleSheet() + """
                QToolButton { padding:0px; font-size:12px; }
                QToolButton:checked { padding:0px; }
            """)
        button.setCheckable(True)
        button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        button.setAccessibleName(title)
        if value in SHORTS_PRESETS:
            photoshop = value.startswith("photoshop_")
            badge = QLabel("Ps" if photoshop else "3D", button)
            badge.setObjectName("presetApplicationBadge")
            badge.setFixedSize(20, 14)
            badge.move(4, 4)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            badge.setStyleSheet(
                "QLabel { background:#111b25; border:0; border-radius:3px; "
                "color:#a9d5ff; font:600 9px 'Segoe UI'; padding:0; }"
            )
            title += " · Photoshop" if photoshop else " · 3D / ZBrush"
            button.setAccessibleName(title)
        description = tr(PRESET_DESCRIPTIONS.get(value, ""))
        button.setToolTip(f"{title}\n{description}" if description else title)
        button.setMinimumSize(44, 52)
        button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._group.addButton(button, len(self._items))
        self._items.append((title, value))
        self.buttons[value] = button
        column = len(self._items) - 1
        self._row.addWidget(button, 0, column)
        self._row.setColumnStretch(column, 1)
        if self._index < 0:
            self.setCurrentIndex(0)

    def activate(self, index: int) -> None:
        if index == self._index:
            return
        self.about_to_activate.emit()
        self.setCurrentIndex(index)
        self.activated.emit()

    def findData(self, value: str) -> int:
        return next((i for i, (_, data) in enumerate(self._items) if data == value), -1)

    def currentData(self) -> str:
        return self._items[self._index][1]

    def setCurrentIndex(self, index: int) -> None:
        if 0 <= index < len(self._items):
            button = self._group.button(index)
            if button is not None:
                button.setChecked(True)
            if self._index != index:
                self._index = index
                self.currentIndexChanged.emit(index)

    def set_shorts_mode(self, shorts: bool) -> None:
        for button in self.buttons.values():
            self._row.removeWidget(button)
        if not self._slots:
            for column in range(len(SHORTS_PRESETS)):
                slot = QWidget(self)
                slot.setStyleSheet("background:transparent;")
                slot.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                self._row.addWidget(slot, 0, column)
                self._slots.append(slot)
        for column in range(len(self._items)):
            self._row.setColumnStretch(column, 1 if column < len(SHORTS_PRESETS) else 0)
            self._row.setColumnMinimumWidth(column, 44 if column < len(SHORTS_PRESETS) else 0)
        column = 0
        for value, button in self.buttons.items():
            visible = (value in SHORTS_PRESETS) == shorts
            button.setVisible(visible)
            if visible:
                self._row.addWidget(button, 0, column)
                button.raise_()
                column += 1
