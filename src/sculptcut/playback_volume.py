"""Shared preview loudness; independent of project and export audio settings."""

from PySide6.QtCore import QObject, QSettings, QSignalBlocker, QSize, Qt, Signal
from PySide6.QtMultimedia import QAudioOutput
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QSlider, QWidget

from sculptcut.i18n import tr
from sculptcut.ui_icons import ui_icon


class PlaybackVolume(QObject):
    changed = Signal()
    volume_changed = Signal(float)
    muted_changed = Signal(bool)

    def __init__(self, settings: QSettings | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        raw_level = settings.value("playback_volume", 50) if settings is not None else 50
        try:
            self.level = max(0, min(100, int(str(raw_level))))
        except (ValueError, TypeError):
            self.level = 50
        self.muted = (
            str(settings.value("playback_muted", False)).lower() == "true"
            if settings is not None else False
        )
        self.last_audible = self.level or 50

    @property
    def silent(self) -> bool:
        return self.muted or self.level == 0

    def bind(self, audio: QAudioOutput) -> None:
        audio.setVolume(self.level / 100)
        audio.setMuted(self.silent)
        self.volume_changed.connect(audio.setVolume)
        self.muted_changed.connect(audio.setMuted)

    def set_level(self, value: int) -> None:
        self.level = max(0, min(100, value))
        if self.level:
            self.last_audible = self.level
        self.muted = False
        self.publish()

    def toggle_mute(self) -> None:
        if self.silent:
            self.level = self.level or self.last_audible
            self.muted = False
        else:
            self.muted = True
        self.publish()

    def publish(self) -> None:
        if self.settings is not None:
            self.settings.setValue("playback_volume", self.level)
            self.settings.setValue("playback_muted", self.muted)
            self.settings.sync()
        self.volume_changed.emit(self.level / 100)
        self.muted_changed.emit(self.silent)
        self.changed.emit()


class VolumeControl(QWidget):
    def __init__(self, state: PlaybackVolume, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = state
        self.setObjectName("playbackVolume")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.mute_button = QPushButton()
        self.mute_button.setFixedSize(32, 32)
        self.mute_button.setIconSize(QSize(20, 20))
        self.mute_button.setCheckable(True)
        self.mute_button.setProperty("actionRole", "utility")
        self.mute_button.setStyleSheet("QPushButton { padding:0; }")
        self.mute_button.clicked.connect(state.toggle_mute)
        layout.addWidget(self.mute_button)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setPageStep(10)
        self.slider.setFixedSize(68, 24)
        self.slider.setAccessibleName(tr("Громкость воспроизведения"))
        self.slider.valueChanged.connect(state.set_level)
        layout.addWidget(self.slider)
        self.setFixedSize(104, 36)
        state.changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        blocker = QSignalBlocker(self.slider)
        self.slider.setValue(self.state.level)
        del blocker
        self.mute_button.setChecked(self.state.silent)
        self.mute_button.setIcon(ui_icon("muted" if self.state.silent else "speaker"))
        label = tr("Включить звук" if self.state.silent else "Выключить звук")
        self.mute_button.setAccessibleName(label)
        self.mute_button.setToolTip(label)
        self.slider.setToolTip(tr("Громкость воспроизведения: {0}%").format(self.state.level))
