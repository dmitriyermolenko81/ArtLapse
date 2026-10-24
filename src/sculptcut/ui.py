"""Local PySide6 desktop interface backed by the same services as the CLI."""

import json
import logging
import re
import secrets
import sys
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path
from threading import Event
from time import monotonic
from typing import Literal, cast

from PySide6.QtCore import (
    QEvent,
    QLocale,
    QObject,
    QPoint,
    QRect,
    QSettings,
    QSize,
    QStandardPaths,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QCloseEvent,
    QColor,
    QDesktopServices,
    QFocusEvent,
    QIcon,
    QImage,
    QKeyEvent,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
    QResizeEvent,
    QValidator,
    QWheelEvent,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame, QVideoSink
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QButtonGroup,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from sculptcut.analysis import analysis_key, extract_features
from sculptcut.compact_label import CompactLabel
from sculptcut.crop_dialog import TimelapseCropDialog, WebcamDialog
from sculptcut.cut_save_dialog import CutSaveDialog
from sculptcut.domain import (
    DEFAULT_BEAT_SYNC_MODE,
    DEFAULT_BEAT_SYNC_STRENGTH,
    Project,
    ProjectSettings,
    Roi,
)
from sculptcut.edit_history import EditHistory
from sculptcut.edit_stats import statistics_text
from sculptcut.episode_panel import EpisodePanel, episode_export
from sculptcut.export_storage import has_exports, ordered_exports
from sculptcut.i18n import language, tr
from sculptcut.localization import error_text, stage_text
from sculptcut.logging_config import configure_logging
from sculptcut.media import Cancelled
from sculptcut.music_library import EXTENSIONS
from sculptcut.output_cut_editor import OutputCutEditor
from sculptcut.output_naming import clean_output_name, next_output_path, output_suffix
from sculptcut.planning import plan_project
from sculptcut.playback_volume import PlaybackVolume, VolumeControl
from sculptcut.popover import exec_anchored
from sculptcut.preflight import check_creation
from sculptcut.preset_selector import PresetSelector
from sculptcut.project import (
    create_project,
    database,
    load_project,
    relink_sources,
    replace_sources,
    resolve_snapshot_sources,
    save_project,
)
from sculptcut.proxy import make_proxies, proxy_path
from sculptcut.rendering import render
from sculptcut.runtime_paths import data_directory
from sculptcut.settings_templates import TemplateOptions
from sculptcut.shorts_layout import MUSIC_SHORTS_PRESETS, SHORTS_PRESETS
from sculptcut.source_cards import (
    META_ROLE,
    PRESENTATION_ROLE,
    SourceCards,
    VideoDropZone,
    load_source_media,
)
from sculptcut.time_format import format_time
from sculptcut.timeline_preview import PreviewSlider
from sculptcut.ui_history import WindowHistory
from sculptcut.ui_icons import ui_icon
from sculptcut.ui_language import install_language, retranslate_windows
from sculptcut.ui_theme import STYLE, style_message_box
from sculptcut.update_dialog import UpdateController
from sculptcut.variants import read_variant

INI_FORMAT = QSettings.Format.IniFormat

EMPTY_VIDEO_HTML = (
    "<span style='font-size:20px;color:#f0f5fd'><b>Здесь появится готовый ролик</b>"
    "</span><br><br>"
    "<span style='color:#9aa8bb'>Добавьте исходники слева, задайте настройки "
    "и нажмите «Создать ролик».</span>"
)
READY_VIDEO_HTML = (
    "<span style='font-size:20px;color:#f0f5fd'><b>Здесь появится готовый ролик</b>"
    "</span><br><br>"
    "<span style='color:#9aa8bb'>Исходники добавлены. Выберите настройки справа "
    "и нажмите «Создать ролик».</span>"
)


def format_hms(seconds: int) -> str:
    """Format the player's time using the shared timeline convention."""
    return format_time(seconds)


class DurationSpinBox(QDoubleSpinBox):
    """Show Shorts seconds as MM:SS once they reach a full minute."""

    def __init__(self) -> None:
        super().__init__()
        self.seconds_mode = False

    def sizeHint(self) -> QSize:
        return QSize(120, 36)

    def minimumSizeHint(self) -> QSize:
        return QSize(80, 36)

    def set_seconds_mode(self, enabled: bool) -> None:
        self.seconds_mode = enabled
        self.setSuffix("" if enabled else tr(" мин"))
        self.setToolTip(
            tr("Длительность вводится в секундах; минуты и секунды подписаны отдельно.")
            if enabled
            else tr("Длительность таймлапса в минутах.")
        )

    def textFromValue(self, value: float) -> str:
        if not self.seconds_mode:
            return f"{value:g}".replace(".", self.locale().decimalPoint())
        seconds = max(0, round(value))
        if seconds < 60:
            return tr(f"{seconds} сек")
        minutes, remainder = divmod(seconds, 60)
        return tr(f"{minutes} мин") + (tr(f" {remainder} сек") if remainder else "")

    def valueFromText(self, text: str) -> float:
        if not self.seconds_mode:
            return super().valueFromText(text)
        value = text.strip().lower()
        minutes_seconds = re.fullmatch(r"(\d+)\s*(?:мин|min)(?:\s*(\d+)\s*(?:сек|sec))?", value)
        if minutes_seconds:
            return float(int(minutes_seconds.group(1)) * 60 + int(minutes_seconds.group(2) or 0))
        plain = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*(?:сек|с|sec|s)?", value)
        if plain:
            return float(plain.group(1).replace(",", "."))
        return super().valueFromText(text)

    def validate(self, text: str, pos: int) -> tuple[QValidator.State, str, int]:
        if not self.seconds_mode:
            return cast(tuple[QValidator.State, str, int], super().validate(text, pos))
        value = text.strip().lower()
        if not value:
            return QValidator.State.Intermediate, text, pos
        minutes_seconds = re.fullmatch(r"(\d+)\s*(?:мин|min)(?:\s*(\d+)\s*(?:сек|sec))?", value)
        if minutes_seconds:
            if int(minutes_seconds.group(2) or 0) >= 60:
                return QValidator.State.Invalid, text, pos
            return QValidator.State.Acceptable, text, pos
        if re.fullmatch(r"\d+\s*(?:м(?:и(?:н)?)?|m(?:i(?:n)?)?)", value):
            return QValidator.State.Intermediate, text, pos
        plain = re.fullmatch(r"\d+(?:[.,]\d+)?\s*(?:сек|с|sec|s)?", value)
        if plain:
            return QValidator.State.Acceptable, text, pos
        return QValidator.State.Invalid, text, pos


class TimeUnitSpinBox(QSpinBox):
    """Commit keyboard/wheel steps immediately so adjacent units carry."""

    committed_text = ""
    neighbor_height = 16

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cycle: int | None = None
        self.neighbor_bounds = (True, True)
        self.valueChanged.connect(self.update)
        editor = self.lineEdit()
        if editor is not None:
            editor.installEventFilter(self)
        self.set_manual_editing(False)

    def set_manual_editing(self, enabled: bool) -> None:
        editor = self.lineEdit()
        if editor is not None:
            editor.setReadOnly(not enabled)
            editor.setCursor(Qt.CursorShape.IBeamCursor if enabled else Qt.CursorShape.ArrowCursor)
            if not enabled:
                editor.deselect()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.lineEdit():
            if (event.type() == QEvent.Type.MouseButtonPress
                    and isinstance(event, QMouseEvent)
                    and event.button() == Qt.MouseButton.LeftButton):
                self.set_manual_editing(True)
            elif event.type() == QEvent.Type.FocusOut:
                self.set_manual_editing(False)
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        editor = self.lineEdit()
        if editor is not None and editor.isReadOnly():
            event.ignore()
            return
        super().keyPressEvent(event)
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Escape):
            self.set_manual_editing(False)

    def focusOutEvent(self, event: QFocusEvent) -> None:
        super().focusOutEvent(event)
        self.set_manual_editing(False)

    def neighbor_text(self, offset: int) -> str:
        if not self.neighbor_bounds[0 if offset < 0 else 1]:
            return ""
        value = self.value() + offset
        if value < self.minimum() or value > self.maximum():
            return ""
        if self.cycle is not None:
            value %= self.cycle
        return str(value)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        editor = self.lineEdit()
        if editor is not None:
            editor.setGeometry(0, self.neighbor_height, self.width(),
                               self.height() - 2 * self.neighbor_height)

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        font = self.font()
        font.setPixelSize(16 if self.neighbor_height == 14 else 18)
        font.setWeight(font.Weight.Normal)
        painter.setFont(font)
        for offset, y in ((-1, 0), (1, self.height() - self.neighbor_height)):
            gradient = QLinearGradient(0, y, 0, y + self.neighbor_height)
            visible = QColor(197, 217, 239, 110 if self.isEnabled() else 40)
            transparent = QColor(197, 217, 239, 0)
            gradient.setColorAt(0, transparent if offset < 0 else visible)
            gradient.setColorAt(0.55 if offset < 0 else 0.45, visible)
            gradient.setColorAt(1, visible if offset < 0 else transparent)
            painter.setPen(QPen(QBrush(gradient), 1))
            painter.drawText(QRect(0, y, self.width(), self.neighbor_height),
                             Qt.AlignmentFlag.AlignCenter, self.neighbor_text(offset))
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            y = event.position().y()
            if y < self.neighbor_height or y >= self.height() - self.neighbor_height:
                self.set_manual_editing(False)
                self.setFocus(Qt.FocusReason.MouseFocusReason)
                self.stepBy(-1 if y < self.neighbor_height else 1)
                event.accept()
                return
            self.set_manual_editing(True)
        super().mousePressEvent(event)

    def event(self, event: QEvent) -> bool:
        # Finished duration edits belong to the window history. Uncommitted text
        # still uses QSpinBox's native undo, like other text fields.
        if (event.type() == QEvent.Type.ShortcutOverride and isinstance(event, QKeyEvent)
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier
                and event.key() in (Qt.Key.Key_Z, Qt.Key.Key_Y)
                and self.text() == self.committed_text):
            event.ignore()
            return False
        return super().event(event)

    def stepBy(self, steps: int) -> None:
        super().stepBy(steps)
        self.editingFinished.emit()

    def wheelEvent(self, event: QWheelEvent) -> None:
        super().wheelEvent(event)
        self.set_manual_editing(False)


class DurationCard(QWidget):
    """Integer time fields with separate controls for each unit."""

    edited = Signal(float, float)

    def __init__(self, target: DurationSpinBox) -> None:
        super().__init__()
        self.target = target
        target.setParent(self)
        target.hide()
        self.setObjectName("durationCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("""
            QWidget#durationCard { background:#202d3d; border:0;
                border-radius:8px; }
            QWidget#durationCard QWidget { background:transparent; }
            QSpinBox { background:transparent; border:0; color:#f0f7ff;
                font:600 30px 'Segoe UI'; padding:0; }
            QLabel { color:#c5d9ef; font:14px 'Segoe UI'; background:transparent; }
            QPushButton { background:#26374b; border:1px solid #445c78;
                border-radius:6px; color:#e4efff; padding:5px 4px; font:14px 'Segoe UI'; }
            QPushButton:hover { background:#355273; border-color:#7db9ff; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(6)
        fields = QHBoxLayout()
        fields.setSpacing(4)
        self.hours_container, self.hours = self.time_field(tr("Часы"))
        hours_policy = self.hours_container.sizePolicy()
        hours_policy.setRetainSizeWhenHidden(True)
        self.hours_container.setSizePolicy(hours_policy)
        self.hours.setMinimum(0)
        self.minutes_container, self.minutes = self.time_field(tr("Минуты"))
        self.seconds_container, self.seconds = self.time_field(tr("Секунды"))
        self.minutes.cycle = self.seconds.cycle = 60
        for container in (self.hours_container, self.minutes_container, self.seconds_container):
            fields.addWidget(container, 1)
        layout.addLayout(fields)
        target.valueChanged.connect(self.sync)
        self.sync()

    def time_field(self, title: str) -> tuple[QWidget, TimeUnitSpinBox]:
        container = QWidget(self)
        column = QVBoxLayout(container)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        field = TimeUnitSpinBox(container)
        field.setRange(-1, 2_147_483_647)
        field.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        field.setAlignment(Qt.AlignmentFlag.AlignCenter)
        field.setMinimumHeight(56)
        field.setMinimumWidth(0)
        field.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        field.setAccessibleName(title)
        field.editingFinished.connect(self.select_duration)
        label = QLabel(title)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(field)
        column.addWidget(label)
        return container, field

    def sync(self) -> None:
        shorts = self.target.seconds_mode
        self.hours_container.setVisible(not shorts)
        total = round(self.target.value() if shorts else self.target.value() * 60)
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        self.minutes.setMinimum(-1 if hours > 0 and not shorts else 0)
        self.seconds.setMinimum(-1 if hours > 0 or minutes > 0 else 0)
        for field, value in ((self.hours, hours), (self.minutes, minutes), (self.seconds, seconds)):
            field.blockSignals(True)
            field.setValue(min(value, field.maximum()))
            field.committed_text = field.text()
            unit = 3600 if field is self.hours else 60 if field is self.minutes else 1
            scale = 1 if shorts else 60
            field.neighbor_bounds = (
                total - unit >= round(self.target.minimum() * scale),
                total + unit <= round(self.target.maximum() * scale),
            )
            field.blockSignals(False)
            field.update()

    def select_duration(self) -> None:
        before = self.target.value()
        total = self.minutes.value() * 60 + self.seconds.value()
        if not self.target.seconds_mode:
            total += self.hours.value() * 3600
        total = max(0, total)
        self.target.setValue(min(300, total) if self.target.seconds_mode else total / 60)
        self.sync()
        self.edited.emit(before, self.target.value())


class Job(QThread):
    result = Signal(object)
    failure = Signal(str)
    progress = Signal(float)
    stage = Signal(str)

    def __init__(
        self,
        operation: Callable[[Event, Callable[[float], None], Callable[[str], None]], str],
    ) -> None:
        super().__init__()
        self.operation = operation
        self.cancel = Event()
        self.last_progress_at = 0.0

    def report_progress(self, value: float) -> None:
        now = monotonic()
        if value < 0 or value >= 1 or now - self.last_progress_at >= 0.1:
            self.progress.emit(value)
            self.last_progress_at = now

    def run(self) -> None:
        try:
            self.result.emit(self.operation(self.cancel, self.report_progress, self.stage.emit))
        except Exception as exc:
            logging.getLogger("artlapse").exception("GUI operation failed")
            self.failure.emit(f"{type(exc).__name__}: {exc}")


class PathPickerLineEdit(QLineEdit):
    """Path display that opens its picker when the field itself is activated."""

    activated = Signal()

    def __init__(self, accessible_name: str) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(accessible_name)
        self.clear_action = self.addAction(
            self.clear_icon(), QLineEdit.ActionPosition.TrailingPosition
        )
        self.clear_action.setToolTip(tr("Очистить"))
        self.clear_action.triggered.connect(self.clear)
        self.clear_action.setVisible(False)
        self.textChanged.connect(lambda text: self.clear_action.setVisible(bool(text)))

    @staticmethod
    def clear_icon() -> QIcon:
        return ui_icon("close", "#e9edf5")

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus()
            self.activated.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activated.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class MainWindow(QMainWindow):
    job_stage = Signal(str)
    statistics_ready = Signal(str)
    encoder_ready = Signal(str)
    source_preview_ready = Signal(str, QImage, str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ArtLapse")
        application_icon = QIcon(str(Path(__file__).parent / "assets" / "logo.ico"))
        QApplication.setWindowIcon(application_icon)
        self.setWindowIcon(application_icon)
        self.resize(1440, 860)
        self.setMinimumSize(1120, 700)
        self.setStyleSheet(STYLE)
        self.job: Job | None = None
        self.edit_history = EditHistory()
        self.episode_panel: EpisodePanel | None = None
        self.last_error = ""
        self.root: Path | None = None
        self.last_result_video: Path | None = None
        self.source_media_cache: dict[str, tuple[QImage, str]] = {}
        self.source_media_pending: set[str] = set()
        self.presentation_video: Path | None = None
        self.presentation_seconds = 5.0
        self.presentation_position: Literal["start", "end"] = "start"
        self.controls: list[QWidget] = []
        self.video_frame_image = QImage()
        self.job_started_at: float | None = None
        self.stage_started_at: float | None = None
        self.current_stage = ""
        self.stage_progress: float | None = None
        self.encoder_name = ""
        self.job_succeeded = False
        settings_data = data_directory()
        self.recent_settings = (
            QSettings(str(settings_data / "settings.ini"), INI_FORMAT)
            if settings_data is not None
            else QSettings("ArtLapse", "ArtLapse")
        )
        if settings_data is None and not self.recent_settings.allKeys():
            # Copy once, preserving the old namespace for older installed versions.
            legacy_settings = QSettings("SculptCut", "SculptCut")
            for key in legacy_settings.allKeys():
                self.recent_settings.setValue(key, legacy_settings.value(key))
            self.recent_settings.sync()
        selected_language = str(self.recent_settings.value("interface_language", "ru"))
        install_language(selected_language if selected_language in ("ru", "en") else "ru")
        self.setLocale(QLocale("ru_RU" if selected_language != "en" else "en_US"))
        self._recover_after_crash = self.recent_settings.value(
            "session_running", False, type=bool,
        )
        self.recent_settings.setValue("session_running", True)
        self.recent_settings.sync()
        self.close_after_job = False
        self._allow_close = False
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 14, 20, 14)
        layout.setSpacing(10)
        heading = QHBoxLayout()
        heading.setSpacing(10)
        self.brand_header = QWidget()
        brand_layout = QHBoxLayout(self.brand_header)
        brand_layout.setContentsMargins(0, 0, 0, 0)
        brand_layout.setSpacing(10)
        brand = QLabel()
        brand.setObjectName("brandIcon")
        brand.setPixmap(
            QPixmap(str(Path(__file__).parent / "assets" / "logo.png")).scaled(
                38,
                38,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        brand_layout.addWidget(brand)
        title = QLabel("ArtLapse")
        title.setObjectName("title")
        brand_layout.addWidget(title)
        brand_layout.addStretch()
        heading.addWidget(self.brand_header)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.timelapse_mode = QPushButton("TIMELAPSE")
        self.shorts_mode = QPushButton("SHORTS")
        self.timelapse_mode.setAccessibleName(tr("Таймлапс"))
        self.shorts_mode.setAccessibleName("Shorts")
        for mode_button in (self.timelapse_mode, self.shorts_mode):
            mode_button.setCheckable(True)
            mode_button.setObjectName("modeButton")
            self.mode_group.addButton(mode_button)
        self.timelapse_mode.setChecked(True)
        self.last_timelapse_preset = "timelapse"
        self.last_shorts_preset = "shorts"
        self.timelapse_mode.clicked.connect(lambda: self.choose_mode("timelapse"))
        self.shorts_mode.clicked.connect(lambda: self.choose_mode("shorts"))
        self.folder = QLineEdit()
        self.folder.setPlaceholderText(tr("Создайте или откройте проект"))
        self.project_header = QWidget()
        project_header_layout = QHBoxLayout(self.project_header)
        project_header_layout.setContentsMargins(0, 0, 0, 0)
        project_header_layout.setSpacing(6)
        project_header_layout.addWidget(self.folder, 1)
        heading.addWidget(self.project_header, 1)
        self.project_actions = QWidget()
        project_actions_layout = QHBoxLayout(self.project_actions)
        project_actions_layout.setContentsMargins(0, 0, 0, 0)
        project_actions_layout.setSpacing(4)
        self.new_project_button = self.button("", self.new_project)
        self.new_project_button.setAccessibleName(tr("Создать проект"))
        self.new_project_button.setIcon(ui_icon("add"))
        self.new_project_button.setShortcut("Ctrl+N")
        self.new_project_button.setToolTip(tr("Создать новый пустой проект (Ctrl+N)"))
        self.open_project_button = self.button("", self.choose_folder)
        self.open_project_button.setAccessibleName(tr("Открыть проект"))
        self.open_project_button.setToolTip(tr("Открыть проект"))
        self.open_project_button.setIcon(ui_icon("folder"))
        self.recent_button = self.button("", self.show_recent_projects)
        self.recent_button.setAccessibleName(tr("Недавние проекты"))
        self.recent_button.setIcon(ui_icon("chevron_down"))
        self.recent_button.setToolTip(tr("Открыть один из недавно использованных проектов"))
        for project_action in (self.new_project_button, self.open_project_button,
                               self.recent_button):
            project_action.setIconSize(QSize(20, 20))
            project_action.setFixedSize(36, 36)
            project_action.setStyleSheet("QPushButton { padding:0; }")
            project_action.setProperty("actionRole", "utility")
            project_header_layout.addWidget(project_action)
        project_actions_layout.addStretch()
        self.templates_button = self.button(tr("Шаблоны"), self.show_templates)
        self.templates_button.setIcon(ui_icon("templates"))
        self.templates_button.setIconSize(QSize(18, 18))
        self.templates_button.setToolTip(tr("Сохранить или применить набор настроек"))
        project_actions_layout.addWidget(self.templates_button)
        self.save_button = self.button(tr("Сохранить"), self.save_current_project)
        self.save_button.setToolTip(tr("Сохранить проект без анализа и экспорта (Ctrl+S)"))
        self.save_button.setShortcut("Ctrl+S")
        project_actions_layout.addWidget(self.save_button)
        self.cleanup_button = self.button("", self.clean_project)
        self.cleanup_button.setAccessibleName(tr("Очистить проект"))
        self.cleanup_button.setToolTip(tr("Очистить файлы или удалить проект"))
        self.cleanup_button.setIcon(ui_icon("trash"))
        self.cleanup_button.setIconSize(QSize(20, 20))
        self.cleanup_button.setFixedSize(36, 36)
        self.cleanup_button.setStyleSheet("QPushButton { padding:0; }")
        project_actions_layout.addWidget(self.cleanup_button)
        self.update_button = QPushButton()
        self.update_button.setAccessibleName(tr("Проверить обновления"))
        self.update_button.setToolTip(tr("Проверить обновления"))
        self.update_button.setIcon(ui_icon("update"))
        self.update_button.setIconSize(QSize(20, 20))
        self.update_button.setFixedSize(36, 36)
        self.update_button.setProperty("actionRole", "utility")
        self.update_button.setStyleSheet("QPushButton { padding:0; }")
        project_actions_layout.addWidget(self.update_button)
        self.updates = UpdateController(self, self.update_button)
        self.language_button = QPushButton(
            selected_language.upper() if selected_language in ("ru", "en") else "RU"
        )
        self.language_button.setObjectName("languageButton")
        self.language_button.setFixedSize(36, 36)
        self.language_button.setToolTip(tr("Язык интерфейса: ") + self.language_button.text())
        self.language_button.setAccessibleName(self.language_button.toolTip())
        self.language_button.setProperty("actionRole", "utility")
        self.language_button.setStyleSheet(
            "QPushButton { padding:0; font-size:11px; font-weight:600; }"
        )
        self.language_button.clicked.connect(
            lambda: self.change_language("en" if language() == "ru" else "ru")
        )
        project_actions_layout.addWidget(self.language_button)
        heading.addWidget(self.project_actions)
        layout.addLayout(heading)
        self.main_splitter = QSplitter()
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.setHandleWidth(10)
        layout.addWidget(self.main_splitter, 1)
        sidebar = QWidget()
        self.setup_panel = sidebar
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(10)
        self.sources_panel = QWidget()
        self.sources_panel.setObjectName("sourcesPanel")
        sources_side = QVBoxLayout(self.sources_panel)
        sources_side.setContentsMargins(16, 14, 16, 14)
        sources_side.setSpacing(8)
        sources_header = QHBoxLayout()
        sources_title = QLabel(tr("Исходники"))
        sources_title.setObjectName("sectionTitle")
        sources_header.addWidget(sources_title)
        sources_header.addStretch()
        self.presentation_button = self.button("", self.choose_presentation)
        self.presentation_button.setAccessibleName(tr("Презентация проекта"))
        self.presentation_button.setFixedSize(36, 36)
        self.presentation_button.setIconSize(QSize(24, 24))
        self.update_presentation_button()
        self.presentation_button.setEnabled(False)
        sources_header.addWidget(self.presentation_button)
        sources_side.addLayout(sources_header)
        self.sources = SourceCards()
        self.sources.files_dropped.connect(self.add_dropped_videos)
        self.sources.setMinimumHeight(180)
        self.sources.setToolTip(
            tr(
                "Ctrl или Shift — выбрать несколько видео. "
                "Презентация выбирается значком у заголовка."
            )
        )
        self.drop_zone = VideoDropZone()
        self.drop_zone.choose_clicked.connect(self.add_videos)
        self.drop_zone.files_dropped.connect(self.add_dropped_videos)
        self.source_stack = QStackedWidget()
        self.source_stack.addWidget(self.drop_zone)
        self.source_stack.addWidget(self.sources)
        sources_side.addWidget(self.source_stack, 1)
        source_actions = QHBoxLayout()
        source_actions.setSpacing(6)
        add_video_button = self.button(tr("Добавить видео"), self.add_videos)
        add_video_button.setIcon(ui_icon("add"))
        add_video_button.setIconSize(QSize(18, 18))
        source_actions.addWidget(add_video_button, 1)
        self.remove_button = self.button(tr("Убрать"), self.remove_sources)
        self.remove_button.setToolTip(
            tr("Убрать выбранные видео из списка. Файлы на диске сохранятся.")
        )
        self.remove_button.setFixedWidth(72)
        source_actions.addWidget(self.remove_button)
        sources_side.addLayout(source_actions)
        self.relink_button = self.button(tr("Найти исходники…"), self.find_missing_clicked)
        self.relink_button.setToolTip(tr("Указать новое расположение перемещённых видео"))
        self.relink_button.hide()
        sources_side.addWidget(self.relink_button)
        side.addWidget(self.sources_panel, 1)
        self.settings_panel = QWidget()
        self.settings_panel.setObjectName("settingsPanel")
        settings_side = QVBoxLayout(self.settings_panel)
        self.settings_layout = settings_side
        settings_side.setContentsMargins(16, 14, 16, 14)
        settings_side.setSpacing(8)
        self.mode_tabs = QWidget()
        self.mode_tabs.setObjectName("modeTabs")
        settings_header = QHBoxLayout(self.mode_tabs)
        settings_header.setContentsMargins(0, 0, 0, 0)
        settings_header.setSpacing(8)
        settings_header.addWidget(self.timelapse_mode, 1)
        settings_header.addWidget(self.shorts_mode, 1)
        settings_side.addWidget(self.mode_tabs)
        self.mode_settings_panel = QWidget()
        self.mode_settings_panel.setObjectName("modeSettingsPanel")
        mode_settings_layout = QVBoxLayout(self.mode_settings_panel)
        self.mode_settings_layout = mode_settings_layout
        mode_settings_layout.setContentsMargins(6, 6, 6, 6)
        mode_settings_layout.setSpacing(0)
        form = QFormLayout()
        self.settings_form = form
        self.settings_dividers: list[QFrame] = []

        def category_divider() -> QFrame:
            line = QFrame()
            line.setObjectName("settingsDivider")
            line.setFixedHeight(1)
            line.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self.settings_dividers.append(line)
            form.addRow(line)
            return line

        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.target = DurationSpinBox()
        self.target.setDecimals(6)
        self.target.setRange(0, (2_147_483_647 * 3600 + 3599) / 60)
        self.target.setSingleStep(1)
        self.target.setValue(10)
        self.target.setSuffix(tr(" мин"))
        self.duration_card = DurationCard(self.target)
        self.duration_divider = category_divider()
        form.addRow(tr("Длительность"), self.duration_card)
        self.duration_label = cast(QLabel, form.labelForField(self.duration_card))
        self.duration_label.setToolTip(tr("Включает заставку и презентационное видео."))
        self.duration_label.setObjectName("settingsCategory")
        self.preset = PresetSelector()
        self.normal_minutes = 10.0
        self.shorts_seconds = 60.0
        self.duration_in_seconds = False
        self.preset.addItem(tr("Таймлапс · эпизоды"), "timelapse")
        self.preset.addItem(tr("Шортс · 9:16"), "shorts")
        self.preset.addItem(tr("Шортс · под музыку · видеоэпизоды"), "music_shorts")
        self.preset.addItem(tr("Шортс · под музыку · кадры"), "beat_shorts")
        self.preset.addItem(tr("Просто ускорение · без вырезок"), "speed")
        self.preset.addItem("Photoshop · 9:16", "photoshop_shorts")
        self.preset.addItem(tr("Photoshop · под музыку · видеоэпизоды"), "photoshop_music_shorts")
        self.preset.addItem(tr("Photoshop · под музыку · кадры"), "photoshop_beat_shorts")
        self.preset.currentIndexChanged.connect(self.preset_changed)
        self.mode_settings_title = QLabel(tr("Настройки таймлапса"))
        self.mode_settings_title.setObjectName("modeSettingsTitle")
        form.insertRow(0, self.mode_settings_title, self.preset)
        self.frame_mode = PresetSelector()
        self.frame_mode.setProperty("controlRole", "frameButtons")
        self.frame_mode.addItem(tr("Полное видео"), "full")
        self.frame_mode.addItem(tr("Только вьюпорт"), "viewport")
        self.frame_mode.setToolTip(
            tr("Для таймлапса: сохранить весь экран или обрезать панели до окна модели.")
        )
        self.crop_roi: Roi = ProjectSettings().timelapse_viewport_roi
        self.crop_customized = False
        self.frame_mode.buttons["viewport"].setToolTip(
            tr("Выбрать область кадра. Нажмите повторно, чтобы изменить рамку.")
        )
        self.frame_mode.buttons["viewport"].clicked.connect(lambda: self.choose_crop_area())
        self.area_stack = QStackedWidget()
        self.area_stack.setObjectName("areaStack")
        self.area_pages: list[QVBoxLayout] = []
        frame_page = QWidget()
        frame_page.setObjectName("areaPage")
        frame_page_layout = QVBoxLayout(frame_page)
        frame_page_layout.setContentsMargins(0, 0, 0, 0)
        self.area_pages.append(frame_page_layout)
        frame_row = QHBoxLayout()
        frame_row.setSpacing(6)
        frame_row.addWidget(self.frame_mode, 1)
        self.frame_mode_label = QLabel(tr("Область кадра"))
        self.frame_mode_label.setObjectName("settingsCategory")
        frame_page_layout.addWidget(self.frame_mode_label)
        frame_page_layout.addLayout(frame_row)
        self.area_stack.addWidget(frame_page)
        self.shorts_layout = PresetSelector()
        self.shorts_layout.setProperty("controlRole", "layoutButtons")
        self.shorts_layout.addItem(tr("Автоматический 9:16"), "auto")
        self.shorts_layout.addItem(tr("Квадрат 1:1 с полями"), "square")
        self.square_rois: dict[Path, Roi] = {}
        self.shorts_layout_label = QLabel(tr("Область видео"))
        self.shorts_layout_label.setObjectName("settingsCategory")
        shorts_page = QWidget()
        shorts_page.setObjectName("areaPage")
        shorts_page_layout = QVBoxLayout(shorts_page)
        shorts_page_layout.setContentsMargins(0, 0, 0, 0)
        self.area_pages.append(shorts_page_layout)
        shorts_page_layout.addWidget(self.shorts_layout_label)
        shorts_area_row = QHBoxLayout()
        shorts_area_row.setSpacing(6)
        shorts_area_row.addWidget(self.shorts_layout, 1)
        shorts_page_layout.addLayout(shorts_area_row)
        self.area_stack.addWidget(shorts_page)
        empty_area_page = QWidget()
        empty_area_page.setObjectName("areaPage")
        empty_area_layout = QVBoxLayout(empty_area_page)
        empty_area_layout.setContentsMargins(0, 0, 0, 0)
        self.area_pages.append(empty_area_layout)
        self.full_frame_label = QLabel(tr("Область кадра"))
        self.full_frame_label.setObjectName("settingsCategory")
        empty_area_layout.addWidget(self.full_frame_label)
        self.full_frame_hint = QLabel(tr("Весь кадр, без обрезки"))
        self.full_frame_hint.setObjectName("settingsSubLabel")
        self.full_frame_hint.setFixedHeight(52)
        self.full_frame_hint.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        empty_area_layout.addWidget(self.full_frame_hint)
        self.area_stack.addWidget(empty_area_page)
        self.frame_divider = category_divider()
        form.addRow(self.area_stack)
        self.webcam_rois: dict[Path, Roi | None] = {}
        self.webcam_enabled = QPushButton()
        self.webcam_enabled.setCheckable(True)
        self.webcam_enabled.setIcon(ui_icon("webcam"))
        self.webcam_enabled.setIconSize(QSize(20, 20))
        self.webcam_enabled.setFixedWidth(38)
        self.webcam_enabled.setStyleSheet("padding:4px;")
        self.webcam_enabled.setProperty("actionRole", "secondary")
        self.webcam_enabled.setAccessibleName(tr("Веб-камера: настроить область"))
        self.webcam_enabled.setToolTip(
            tr("Выбрать или изменить область веб-камеры на каждом исходнике.\n"
            "Для отключения выберите «Нет веб-камеры» в окне области.")
        )
        source_actions.insertWidget(1, self.webcam_enabled)
        self.webcam_enabled.clicked.connect(self.configure_webcam)
        self.webcam_enabled.toggled.connect(self.webcam_toggled)
        self.controls.append(self.webcam_enabled)
        self.shorts_layout.currentIndexChanged.connect(self.square_layout_changed)
        self.output_name = QLineEdit()
        self.output_name.setMaxLength(124)
        self.output_name.setPlaceholderText(tr("Например: Модель дракона"))
        self.output_name.setToolTip(
            tr("Имя готового файла. Суффикс пресета добавится автоматически; "
            "существующий файл не заменяется.")
        )
        self.output_suffix = QLabel("_timelapse.mp4")
        self.output_suffix.setObjectName("subtitle")
        self.music = PathPickerLineEdit(tr("Выбрать папку с музыкой"))
        self.music_seed = secrets.randbits(32)
        self.music.setPlaceholderText(tr("Нажмите, чтобы выбрать папку с музыкой"))
        self.music.setToolTip(tr(
            "Нажмите на поле, чтобы выбрать папку; значок ноты — один файл; × — очистить"
        ))
        self.music.activated.connect(self.choose_music_folder)
        music_row = QWidget()
        music_row.setObjectName("musicPickerRow")
        music_layout = QHBoxLayout(music_row)
        music_layout.setContentsMargins(0, 0, 0, 0)
        music_layout.setSpacing(6)
        music_layout.addWidget(self.music, 1)
        self.music_file_button = QPushButton()
        self.music_file_button.setIcon(ui_icon("music"))
        self.music_file_button.setIconSize(QSize(20, 20))
        self.music_file_button.setFixedSize(36, 36)
        self.music_file_button.setObjectName("musicFileButton")
        self.music_file_button.setToolTip(tr("Выбрать музыкальный файл"))
        self.music_file_button.setAccessibleName(tr("Выбрать музыкальный файл"))
        self.music_file_button.clicked.connect(self.choose_music_file)
        music_layout.addWidget(self.music_file_button)
        self.audio_divider = category_divider()
        form.addRow(tr("Звук"), music_row)
        self.audio_label = cast(QLabel, form.labelForField(music_row))
        self.audio_label.setObjectName("settingsCategory")
        self.music_selection = PresetSelector()
        self.music_selection.setProperty("controlRole", "musicButtons")
        self.music_selection.addItem(tr("Динамический подбор фрагментов"), "dynamic")
        self.music_selection.addItem(tr("Треки целиком по очереди"), "full_tracks")
        self.music_selection.setToolTip(
            tr("Динамический режим ищет энергичный фрагмент трека. "
            "Последовательный воспроизводит каждый трек с начала; переходы плавные.")
        )
        form.addRow(tr("Режим музыки"), self.music_selection)
        self.music_mode_label = cast(QLabel, form.labelForField(self.music_selection))
        self.music_mode_label.setObjectName("settingsSubLabel")
        self.intro_image = PathPickerLineEdit(tr("Выбрать изображение заставки"))
        self.intro_image.setPlaceholderText(tr("Нажмите, чтобы выбрать PNG/JPG"))
        self.intro_image.setToolTip(
            tr("Нажмите на поле, чтобы выбрать другую заставку; × — очистить")
        )
        self.intro_image.activated.connect(self.choose_intro_image)
        self.intro_preview = QLabel()
        self.intro_preview.setObjectName("introPreview")
        self.intro_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.intro_preview.setFixedSize(56, 36)
        self.intro_preview.hide()
        intro_row = QHBoxLayout()
        intro_row.setSpacing(6)
        intro_row.addWidget(self.intro_preview)
        intro_row.addWidget(self.intro_image, 1)
        self.intro_divider = category_divider()
        form.addRow(tr("Заставка"), intro_row)
        self.intro_label = cast(QLabel, form.labelForField(intro_row))
        self.intro_label.setObjectName("settingsCategory")
        self.intro_label.setWordWrap(True)
        mode_settings_layout.addLayout(form)
        settings_side.addWidget(self.mode_settings_panel)
        settings_side.addStretch(1)
        self.sidebar_scroll = QScrollArea()
        self.sidebar_scroll.setMinimumWidth(270)
        self.sidebar_scroll.setMaximumWidth(350)
        self.sidebar_scroll.setWidgetResizable(True)
        self.sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sidebar_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.sidebar_scroll.setWidget(sidebar)
        self.main_splitter.addWidget(self.sidebar_scroll)
        content = QWidget()
        content.setObjectName("viewerPanel")
        right = QVBoxLayout(content)
        right.setContentsMargins(16, 14, 16, 14)
        right.setSpacing(10)
        viewer_header = QHBoxLayout()
        self.video_title = CompactLabel(tr("Готовый ролик"))
        self.video_title.setObjectName("sectionTitle")
        self.video_title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        viewer_header.addWidget(self.video_title, 1)
        self.viewer_context = QLabel(tr("ЕЩЁ НЕ СОЗДАН"))
        self.viewer_context.setObjectName("contextBadge")
        viewer_header.addWidget(self.viewer_context)
        right.addLayout(viewer_header)
        self.video = QLabel(tr(EMPTY_VIDEO_HTML))
        self.video.setTextFormat(Qt.TextFormat.RichText)
        self.video.setWordWrap(True)
        self.video.setObjectName("videoCanvas")
        self.video.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.video.setMinimumHeight(200)
        self.video.installEventFilter(self)
        right.addWidget(self.video, 2)
        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.volume_state = PlaybackVolume(self.recent_settings, self)
        self.volume_state.bind(self.audio_output)
        self.player.setAudioOutput(self.audio_output)
        self.video_sink = QVideoSink(self)
        self.video_sink.videoFrameChanged.connect(self.show_video_frame)
        self.player.setVideoSink(self.video_sink)
        self.player.sourceChanged.connect(self.clear_video_frame)
        self.player.errorOccurred.connect(lambda error, message: self.show_error(message))
        self.video_frames_seen = 0
        self.pause_on_frame = False
        self.pending_seek: int | None = None
        self.episode_end: int | None = None
        self.player.mediaStatusChanged.connect(self.seek_when_loaded)
        self.seek = PreviewSlider()
        self.player.sourceChanged.connect(self.seek.set_source)
        self.seek.seekRequested.connect(self.seek_preview)
        self.seek.setRange(0, 0)
        self.seek.sliderMoved.connect(self.seek_preview)
        self.player.durationChanged.connect(self.update_seek_range)
        self.player.positionChanged.connect(self.update_position)
        self.sources.currentRowChanged.connect(lambda _: self.update_aspect_captions())
        right.addWidget(self.seek)
        self.folder.textChanged.connect(self.invalidate_episodes)
        for signal in (
            self.sources.model().rowsInserted,
            self.sources.model().rowsRemoved,
            self.sources.model().rowsMoved,
        ):
            signal.connect(self.invalidate_episodes)
            signal.connect(self.update_source_summary)
        self.video_time = QLabel("00:00 / 00:00")
        playback = QHBoxLayout()
        playback.setSpacing(8)
        right.addWidget(self.video_time)
        self.volume_control = VolumeControl(self.volume_state)
        playback.addWidget(self.volume_control)
        playback.addStretch(1)
        self.cut_button = self.button(tr("Подрезка ролика"), self.toggle_output_editor)
        self.cut_button.setAccessibleName(tr("Подрезка готового ролика"))
        self.cut_button.setIcon(ui_icon("chevron_up", "#8ed5ca"))
        self.cut_button.setIconSize(QSize(18, 18))
        self.cut_button.setCheckable(True)
        self.cut_button.setProperty("actionRole", "disclosure")
        self.cut_button.setAccessibleDescription(tr("Раскрывает или скрывает панель подрезки"))
        self.cut_button.setToolTip(tr("Открыть инструменты выбора и удаления участков видео"))
        self.cut_button.hide()
        self.play_button = self.button(tr("Просмотр"), self.play_pause)
        self.play_button.setMinimumWidth(112)
        self.play_button.setIcon(ui_icon("play"))
        self.play_button.setIconSize(QSize(18, 18))
        self.play_button.setToolTip(tr("Воспроизвести видео или поставить его на паузу"))
        self.player.playbackStateChanged.connect(self.playback_state_changed)
        playback.addWidget(self.play_button)
        self.create_button = self.button(tr("Создать ролик"), self.create_or_export)
        self.create_button.setObjectName("primary")
        self.create_button.setToolTip(
            tr("Анализ, автоматический монтаж и экспорт; затем можно подрезать MP4")
        )
        self.change_music_button = self.button(tr("Сменить музыку"), self.change_music_from_button)
        self.change_music_button.setIcon(ui_icon("music"))
        self.change_music_button.setIconSize(QSize(19, 19))
        self.change_music_button.setProperty("actionRole", "secondary")
        self.change_music_button.setToolTip(
            tr("Выберите музыку справа и создайте версию ролика с другим звуком")
        )
        self.change_intro_button = self.button(
            tr("Обновить заставку"), self.change_intro_from_button
        )
        self.change_intro_button.setIcon(ui_icon("image"))
        self.change_intro_button.setIconSize(QSize(19, 19))
        self.change_intro_button.setProperty("actionRole", "secondary")
        self.change_intro_button.setToolTip(
            tr("Выберите изображение заставки справа и создайте новую версию ролика")
        )
        self.outputs_button = QPushButton(tr("Открыть готовые видео"))
        self.outputs_button.setIcon(ui_icon("folder"))
        self.outputs_button.setIconSize(QSize(19, 19))
        self.outputs_button.setProperty("actionRole", "utility")
        self.outputs_button.setToolTip(tr("Открыть папку с уже созданными MP4"))
        self.outputs_button.clicked.connect(self.open_outputs)
        self.music.textChanged.connect(self.music_path_changed)
        self.intro_image.textChanged.connect(self.intro_path_changed)
        self.folder.textChanged.connect(self.update_result_actions)
        right.addLayout(playback)
        self.cut_section = QWidget()
        self.cut_section.setObjectName("cutSection")
        cut_layout = QVBoxLayout(self.cut_section)
        cut_layout.setContentsMargins(1, 1, 1, 1)
        cut_layout.setSpacing(0)
        cut_layout.addWidget(self.cut_button)
        right.addWidget(self.cut_section)
        self.versions_panel = QWidget()
        self.versions_panel.setObjectName("versionsPanel")
        versions_layout = QHBoxLayout(self.versions_panel)
        versions_layout.setContentsMargins(6, 4, 6, 4)
        versions_layout.setSpacing(6)
        versions_description = (
            tr("Выбор открывает сохранённый ролик. «Применить» возвращает настройки "
            "его сборки для следующего монтажа. Существующие MP4 сохраняются.")
        )
        self.versions_panel.setToolTip(versions_description)
        versions_title = QLabel(tr("Версия"))
        versions_title.setObjectName("sectionHint")
        versions_title.setToolTip(versions_description)
        versions_layout.addWidget(versions_title)
        self.variants = QComboBox()
        self.variants.setObjectName("versionsChoice")
        self.variants.setProperty("controlRole", "menu")
        self.variants.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.variants.setMinimumContentsLength(1)
        self.variants.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.variants.setPlaceholderText(tr("Выберите готовый ролик"))
        self.variants.setToolTip(tr("Открыть одну из ранее созданных версий"))
        self.variants.activated.connect(self.open_variant)
        self.variants.currentIndexChanged.connect(self.update_variant_tooltip)
        self.controls.append(self.variants)
        versions_layout.addWidget(self.variants, 1)
        restore_button = self.button(tr("Применить"), self.restore_variant)
        restore_button.setAccessibleName(tr("Применить настройки выбранной версии"))
        restore_button.setProperty("actionRole", "secondary")
        restore_button.setToolTip(tr("Вернуть параметры, с которыми была создана выбранная версия"))
        versions_layout.addWidget(restore_button)
        self.delete_variant_button = self.button("", self.delete_selected_variant)
        self.delete_variant_button.setObjectName("deleteVariant")
        self.delete_variant_button.setAccessibleName(tr("Удалить выбранную версию"))
        self.delete_variant_button.setToolTip(tr("Удалить выбранную версию"))
        self.delete_variant_button.setIcon(ui_icon("trash", "#d6a4ab"))
        self.delete_variant_button.setIconSize(QSize(19, 19))
        self.delete_variant_button.setFixedSize(32, 32)
        self.delete_variant_button.setStyleSheet(
            "QPushButton { padding:0; }"
            "QPushButton:hover { background:#4a2b35; border-color:#bc7787; }"
        )
        self.delete_variant_button.setEnabled(False)
        versions_layout.addWidget(self.delete_variant_button)
        versions_info = QPushButton()
        versions_info.setIcon(ui_icon("info", "#c1d0e0"))
        versions_info.setIconSize(QSize(22, 22))
        versions_info.setAccessibleName(tr("Справка о готовых версиях"))
        versions_info.setFixedSize(28, 28)
        versions_info.setStyleSheet(
            "QPushButton { padding:0; border:0; border-radius:14px; background:transparent; }"
            "QPushButton:hover { background:#344457; }"
            "QPushButton:pressed { background:#243448; }"
            "QPushButton:focus { border:1px solid #a9cbff; }"
        )
        versions_info.setToolTip(versions_description)
        versions_info.clicked.connect(lambda: QToolTip.showText(
            versions_info.mapToGlobal(QPoint(0, versions_info.height())),
            versions_description, versions_info,
        ))
        versions_layout.addWidget(versions_info)
        self.versions_panel.hide()
        right.addWidget(self.versions_panel)
        self.folder.textChanged.connect(self.refresh_variants)
        self.output_editor = OutputCutEditor(self.edit_history)
        self.controls.append(self.output_editor)
        cut_layout.insertWidget(0, self.output_editor)
        self.cut_section.hide()
        self.output_editor.timeline.seek_requested.connect(self.seek_cut_preview)
        self.output_editor.timeline.hover_requested.connect(self.seek.show_hover)
        self.output_editor.timeline.hover_finished.connect(self.seek.hide_hover)
        self.output_editor.timeline.view_changed.connect(self.cut_view_changed)
        self.output_editor.changed.connect(self.refresh_cut_preview)
        self.seek.source_time = self.output_source_time
        self.output_editor.save_requested.connect(self.save_output_cuts)
        self.player.positionChanged.connect(self.output_editor.set_position)
        self.player.durationChanged.connect(self.output_editor.set_duration)
        self.player.sourceChanged.connect(self.reset_output_editor)
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        right_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        right_scroll.setWidget(content)
        self.viewer_scroll = right_scroll
        self.main_splitter.addWidget(right_scroll)
        self.settings_scroll = QScrollArea()
        self.settings_scroll.setMinimumWidth(300)
        self.settings_scroll.setMaximumWidth(360)
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.settings_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.settings_scroll.setWidget(self.settings_panel)
        self.settings_scroll.viewport().installEventFilter(self)
        self.main_splitter.addWidget(self.settings_scroll)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setStretchFactor(2, 0)
        self.main_splitter.setSizes([310, 760, 330])
        self.sidebar_scroll.installEventFilter(self)
        self.settings_scroll.installEventFilter(self)
        self.main_splitter.splitterMoved.connect(self.align_project_header)
        result_actions_bar = QWidget()
        result_actions_bar.setObjectName("resultActionsBar")
        result_actions = QHBoxLayout(result_actions_bar)
        result_actions.setContentsMargins(12, 6, 12, 6)
        result_actions.setSpacing(8)
        result_label = QLabel(tr("Работа с готовым роликом"))
        result_label.setObjectName("sectionHint")
        result_actions.addWidget(result_label)
        result_actions.addStretch(1)
        self.episodes_button = self.button(tr("Эпизоды монтажа"), self.show_episodes)
        self.episodes_button.setProperty("actionRole", "secondary")
        self.episodes_button.setToolTip(
            tr("Миниатюры эпизодов и точная правка границ автоматического монтажа")
        )
        result_actions.addWidget(self.episodes_button)
        result_actions.addWidget(self.change_music_button)
        result_actions.addWidget(self.change_intro_button)
        result_actions.addWidget(self.outputs_button)
        layout.addWidget(result_actions_bar)
        export_bar = QWidget()
        export_bar.setObjectName("exportBar")
        export_layout = QHBoxLayout(export_bar)
        export_layout.setContentsMargins(12, 10, 12, 10)
        export_layout.setSpacing(8)
        export_label = QLabel(tr("Имя файла"))
        export_label.setObjectName("eyebrow")
        export_layout.addWidget(export_label)
        export_layout.addWidget(self.output_name, 1)
        export_layout.addWidget(self.output_suffix)
        self.create_button.setMinimumWidth(210)
        export_layout.addWidget(self.create_button)
        layout.addWidget(export_bar)
        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.status = CompactLabel(tr("Готово к работе"))
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bottom.addWidget(self.status, 1)
        self.error_details_button = QPushButton(tr("Подробности ошибки"))
        self.error_details_button.hide()
        self.error_details_button.clicked.connect(self.show_error_details)
        bottom.addWidget(self.error_details_button)
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setMinimumWidth(220)
        self.progress_bar.setMaximumWidth(360)
        self.progress_bar.hide()
        bottom.addWidget(self.progress_bar)
        self.cancel_button = QPushButton(tr("Отмена"))
        self.cancel_button.setEnabled(False)
        self.cancel_button.hide()
        self.cancel_button.clicked.connect(self.cancel_job)
        bottom.addWidget(self.cancel_button)
        layout.addLayout(bottom)
        self.statistics = QLabel("")
        self.statistics.setWordWrap(True)
        self.statistics.setObjectName("subtitle")
        self.statistics.setToolTip(
            tr("Вырезано — исключённое время исходников. Ускорение не считается удалением.")
        )
        self.statistics.hide()
        self.statistics_ready.connect(self.show_statistics)
        layout.addWidget(self.statistics)
        self.progress_details = QLabel(tr("Ожидание"), central)
        self.progress_details.setObjectName("subtitle")
        self.progress_details.setWordWrap(True)
        self.progress_details.hide()
        layout.addWidget(self.progress_details)
        self.progress_timer = QTimer(self)
        self.progress_timer.setInterval(1000)
        self.progress_timer.timeout.connect(self.update_progress_details)
        self.encoder_ready.connect(self.set_encoder_name)
        self.controls.extend(
            [
                self.folder,
                self.sources,
                self.target,
                self.output_name,
                self.music,
                self.music_file_button,
                self.music_selection,
                self.intro_image,
                self.preset,
                self.frame_mode,
                self.shorts_layout,
                self.timelapse_mode,
                self.shorts_mode,
                self.duration_card,
                self.drop_zone,
            ]
        )
        for widget in self.controls:
            if isinstance(widget, (QLineEdit, QDoubleSpinBox, QPushButton, QComboBox)):
                widget.setMinimumHeight(36)
        self.target.valueChanged.connect(self.invalidate_episodes)
        self.frame_mode.currentIndexChanged.connect(self.frame_area_changed)
        self.output_name.textChanged.connect(self.invalidate_episodes)
        self.music.textChanged.connect(self.invalidate_episodes)
        self.intro_image.textChanged.connect(self.invalidate_episodes)
        self.update_source_summary()
        self.sources.itemSelectionChanged.connect(self.update_result_actions)
        self.update_result_actions()
        self.sync_mode_controls()
        self.update_playback_controls()
        self._draft_last = ""
        self.draft_timer = QTimer(self)
        self.draft_timer.setInterval(1000)
        self.draft_timer.timeout.connect(self.autosave_draft)
        self.draft_timer.start()
        self.history_controller = WindowHistory(self)
        for history_button in (
            self.history_controller.undo_button, self.history_controller.redo_button,
        ):
            history_button.setParent(self)
            history_button.hide()
        self.folder.textChanged.connect(self.update_result_actions)
        QTimer.singleShot(0, self.align_project_header)

    def change_language(self, code: str) -> None:
        install_language(code)
        self.language_button.setText(code.upper())
        self.language_button.setToolTip(tr("Язык интерфейса: ") + code.upper())
        self.language_button.setAccessibleName(self.language_button.toolTip())
        self.recent_settings.setValue("interface_language", code)
        self.recent_settings.sync()
        retranslate_windows()
        self.current_stage = tr(self.current_stage)
        self.encoder_name = tr(self.encoder_name)
        self.history_controller.refresh()
        self.duration_card.sync()
        self.update_source_summary()
        self.update_playback_controls()
        self.align_project_header()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (hasattr(self, "settings_scroll")
                and watched in (self.sidebar_scroll, self.settings_scroll)
                and event.type() == QEvent.Type.Resize):
            self.align_project_header()
        if hasattr(self, "video") and watched is self.video and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self.fit_video_frame)
        if (hasattr(self, "settings_scroll")
                and watched is self.settings_scroll.viewport()
                and event.type() == QEvent.Type.Resize):
            QTimer.singleShot(0, self.fit_settings_layout)
        return super().eventFilter(watched, event)

    def align_project_header(self) -> None:
        """Keep the header columns on the splitter's live panel boundaries."""
        self.brand_header.setFixedWidth(self.sidebar_scroll.width())
        self.project_actions.setFixedWidth(self.settings_scroll.width())

    def fit_settings_layout(self) -> None:
        self.update_aspect_captions()
        compact = self.settings_scroll.viewport().height() < 600
        self.settings_layout.setContentsMargins(0, 0, 0, 0)
        self.settings_layout.setSpacing(0)
        self.mode_settings_layout.setContentsMargins(*((12, 2, 12, 0) if compact
                                                      else (16, 14, 16, 14)))
        for mode_button in (self.timelapse_mode, self.shorts_mode):
            mode_button.setFixedHeight(40)
        self.settings_form.setVerticalSpacing(2 if compact else 8)
        for area_layout in self.area_pages:
            area_layout.setSpacing(3 if compact else 8)
        label_height = max(self.frame_mode_label.sizeHint().height(),
                           self.shorts_layout_label.sizeHint().height())
        self.area_stack.setFixedHeight(label_height + (3 if compact else 8)
                                       + 52)
        for selector in (self.preset, self.frame_mode, self.shorts_layout,
                         self.music_selection):
            for button in selector.buttons.values():
                button.setMinimumWidth(44 if compact or selector is self.preset else 52)
                format_button = button.toolButtonStyle() == (
                    Qt.ToolButtonStyle.ToolButtonTextUnderIcon
                )
                button.setFixedHeight(52 if format_button else
                                      40 if compact else 52)
                icon_size = 24 if compact else 28
                button.setIconSize(QSize(icon_size, icon_size))
        self.update_preview_minimum()
        duration_layout = self.duration_card.layout()
        if duration_layout is not None:
            duration_layout.setContentsMargins(8, 1 if compact else 6, 8, 1 if compact else 6)
        for field in (self.duration_card.hours, self.duration_card.minutes,
                      self.duration_card.seconds):
            field.neighbor_height = 14 if compact else 16
            field.setMinimumHeight(56 if compact else 72)
            field.setStyleSheet("font:600 24px 'Segoe UI';" if compact else "")

    def update_preview_minimum(self) -> None:
        """Let trimming borrow preview height without raising the viewer's minimum."""
        height = 120 if self.settings_scroll.viewport().height() < 600 else 200
        if self.output_editor.isVisible():
            height = max(40, height - self.output_editor.minimumSizeHint().height())
        self.video.setMinimumHeight(height)

    def form_draft(self) -> dict[str, object]:
        return {
            "webcam_enabled": self.webcam_enabled.isChecked(),
            "webcam_rois": {str(p): roi.model_dump() if roi else None
                            for p, roi in self.webcam_rois.items()},
            "schema": 1, "folder": self.folder.text(),
            "sources": [str(p) for p in self.listed_source_paths()],
            "preset": self.preset.currentData(), "target": self.target.value(),
            "normal_minutes": self.normal_minutes, "shorts_seconds": self.shorts_seconds,
            "output_name": self.output_name.text(), "music": self.music.text(),
            "intro": self.intro_image.text(), "music_seed": self.music_seed,
            "presentation_video": str(self.presentation_video or ""),
            "presentation_seconds": self.presentation_seconds,
            "presentation_position": self.presentation_position,
            "presentation_source_index": self.presentation_values()["presentation_source_index"],
            "music_selection": self.music_selection.currentData(),
            "frame_mode": self.frame_mode.currentData(),
            "crop_roi": self.crop_roi.model_dump(), "crop_customized": self.crop_customized,
            "shorts_layout": self.shorts_layout.currentData(),
            "square_rois": {str(p): roi.model_dump() for p, roi in self.square_rois.items()},
        }

    def template_options(self) -> "TemplateOptions":
        return TemplateOptions.model_validate({
            "preset": self.preset.currentData(),
            "target_seconds": (self.target.value() if self.duration_in_seconds
                               else self.target.value() * 60),
            "normal_minutes": (self.normal_minutes if self.duration_in_seconds
                               else self.target.value()),
            "shorts_seconds": (self.target.value() if self.duration_in_seconds
                               else self.shorts_seconds),
            "frame_mode": self.frame_mode.currentData(),
            "crop_roi": self.crop_roi.model_dump(), "crop_customized": self.crop_customized,
            "shorts_layout": self.shorts_layout.currentData(),
            "webcam_enabled": self.webcam_enabled.isChecked(),
            "music": self.music.text(), "music_selection": self.music_selection.currentData(),
            "intro": self.intro_image.text(), "presentation_seconds": self.presentation_seconds,
            "presentation_position": self.presentation_position,
        })

    def apply_template_options(self, options: "TemplateOptions") -> None:
        self.preset.blockSignals(True)
        self.preset.setCurrentIndex(self.preset.findData(options.preset))
        self.preset.blockSignals(False)
        self.preset_changed()
        self.target.setValue(options.target_seconds if self.duration_in_seconds
                             else options.target_seconds / 60)
        self.normal_minutes, self.shorts_seconds = options.normal_minutes, options.shorts_seconds
        self.crop_roi = options.crop_roi.model_copy()
        self.crop_customized = options.crop_customized
        self.frame_mode.setCurrentIndex(self.frame_mode.findData(options.frame_mode))
        self.shorts_layout.setCurrentIndex(self.shorts_layout.findData(options.shorts_layout))
        self.webcam_enabled.blockSignals(True)
        self.webcam_enabled.setChecked(options.webcam_enabled)
        self.webcam_enabled.blockSignals(False)
        self.music.setText(options.music)
        self.music_selection.setCurrentIndex(self.music_selection.findData(options.music_selection))
        self.intro_image.setText(options.intro)
        self.presentation_seconds = options.presentation_seconds
        self.presentation_position = options.presentation_position
        self.update_presentation_button()
        self.update_aspect_captions()
        self.duration_card.sync()
        self.refresh_cut_preview()
        self.history_controller.pending_timing = self.history_controller.timing()
        self.history_controller.pending_framing = self.history_controller.framing()
        self.autosave_draft()

    def show_templates(self) -> None:
        if self.job is not None:
            return
        from sculptcut.template_dialog import TemplateDialog

        dialog = TemplateDialog(self.recent_settings, self.template_options, self)
        result = exec_anchored(dialog, self.templates_button)
        chosen = dialog.chosen
        dialog.deleteLater()
        if result != QDialog.DialogCode.Accepted or chosen is None:
            return
        before = (self.template_options(), self.last_timelapse_preset, self.last_shorts_preset)

        def apply(state: tuple[TemplateOptions, str, str]) -> None:
            self.apply_template_options(state[0])
            self.last_timelapse_preset, self.last_shorts_preset = state[1:]

        self.apply_template_options(chosen.options)
        after = (self.template_options(), self.last_timelapse_preset, self.last_shorts_preset)
        self.edit_history.record(tr("Шаблон настроек"), before, after, apply)
        self.status.setText(tr(f"Шаблон «{chosen.name}» применён."))
        self.status.setToolTip("")
        missing = [path for path in (chosen.options.music, chosen.options.intro)
                   if path and not Path(path).exists()]
        if missing:
            self.status.setText(self.status.text() + tr(" Некоторые файлы недоступны."))
            self.status.setToolTip("\n".join(missing))

    def autosave_draft(self) -> None:
        # A draft also accepts incomplete forms; it never changes project.json.
        draft = json.dumps(self.form_draft(), ensure_ascii=False, sort_keys=True)
        if draft == self._draft_last:
            return
        self.recent_settings.setValue("session_draft", draft)
        self.recent_settings.sync()
        if self.recent_settings.status() != self.recent_settings.Status.NoError:
            self.show_error(tr("Не удалось автосохранить настройки. Проверьте доступ к диску."))
            return
        self._draft_last = draft

    def recover_session(self) -> None:
        if not self._recover_after_crash:
            return
        raw = self.recent_settings.value("session_draft", "")
        if not raw:
            return
        self.draft_timer.stop()
        try:
            draft = json.loads(str(raw))
            if not isinstance(draft, dict) or draft.get("schema") != 1:
                raise ValueError(tr("Неизвестный формат автосохранения"))
            if draft.get("preset") == "locked_beat_shorts":
                draft["preset"] = "beat_shorts"
            # Validate the entire snapshot before changing the visible form.
            folder = str(draft["folder"])
            paths = [Path(p) for p in draft["sources"]]
            roi = Roi.model_validate(draft["crop_roi"])
            squares = {Path(p): Roi.model_validate(r) for p, r in draft["square_rois"].items()}
            webcams = {Path(p): Roi.model_validate(r) if r else None
                       for p, r in draft.get("webcam_rois", {}).items()}
            target = float(draft["target"])
            normal = float(draft["normal_minutes"])
            shorts = float(draft["shorts_seconds"])
            seed = int(draft["music_seed"])
            presentation = ProjectSettings.model_validate({
                "presentation_video": draft.get("presentation_video") or None,
                "presentation_seconds": draft.get("presentation_seconds", 5),
                "presentation_position": draft.get("presentation_position", "start"),
                "presentation_source_index": draft.get("presentation_source_index", 0),
            })
            for combo, key in ((self.preset, "preset"), (self.frame_mode, "frame_mode"),
                               (self.shorts_layout, "shorts_layout"),
                               (self.music_selection, "music_selection")):
                if combo.findData(draft[key]) < 0:
                    raise ValueError(tr("Неизвестные настройки автосохранения"))
            defaults = self.form_draft()
            if all(draft.get(key) == value for key, value in defaults.items()
                   if key != "music_seed"):
                return
            if not self.confirm_action(tr("Восстановить настройки?"),
                                       tr("Найдены сохранённые настройки последнего сеанса."),
                                       tr("Восстановить"), tr("Начать заново")):
                self.recent_settings.remove("session_draft")
                self.recent_settings.sync()
                return
            self.folder.setText(folder)
            self.root = Path(folder).resolve() if folder else None
            self.set_sources(paths)
            self.preset.setCurrentIndex(self.preset.findData(draft["preset"]))
            self.frame_mode.setCurrentIndex(self.frame_mode.findData(draft["frame_mode"]))
            self.shorts_layout.setCurrentIndex(self.shorts_layout.findData(draft["shorts_layout"]))
            self.crop_roi = roi
            self.crop_customized = bool(draft["crop_customized"])
            self.square_rois = squares
            self.webcam_rois = webcams
            self.webcam_enabled.blockSignals(True)
            self.webcam_enabled.setChecked(bool(draft.get("webcam_enabled", False)))
            self.webcam_enabled.blockSignals(False)
            self.target.setValue(target)
            self.normal_minutes, self.shorts_seconds = normal, shorts
            self.output_name.setText(str(draft["output_name"]))
            self.music.setText(str(draft["music"]))
            self.intro_image.setText(str(draft["intro"]))
            self.restore_presentation(presentation)
            self.music_seed = seed
            self.music_selection.setCurrentIndex(self.music_selection.findData(draft["music_selection"]))
            self.status.setText(
                tr("Настройки восстановлены. Можно сохранить проект или продолжить работу.")
            )
            self.autosave_draft()
        except (ValueError, TypeError, KeyError, OSError, AttributeError) as exc:
            self.show_error(tr(f"Не удалось восстановить настройки: {exc}"))
        finally:
            self.draft_timer.start()

    def save_current_project(self) -> None:
        if self.job:
            return
        if self.find_missing_sources(self.save_current_project):
            return
        if not self.folder.text().strip() or not self.source_paths():
            self.show_error(tr("Выберите папку проекта и добавьте хотя бы одно видео для монтажа."))
            return
        try:
            name = clean_output_name(self.output_name.text())
            target = self.target.value() if self.duration_in_seconds else self.target.value() * 60
            if target <= 0:
                raise ValueError(tr("Укажите длительность больше нуля."))
            root = Path(self.folder.text()).resolve()
            paths = self.source_paths()
            music = Path(self.music.text()).resolve() if self.music.text().strip() else None
            preset = self.preset.currentData()
            squares = self.square_rois.copy()
            webcams = self.webcam_rois.copy()
            values = {
                "webcam_enabled": self.webcam_enabled.isChecked(),
                "target_seconds": target, "preset": preset, "output_name": name,
                "crop_mode": self.frame_mode.currentData() if preset == "timelapse" else "full",
                "timelapse_viewport_roi": self.crop_roi,
                "shorts_layout": self.shorts_layout.currentData(),
                "sculpt_only": preset in ("shorts", "beat_shorts", "music_shorts"),
                "music_path": music if music and music.is_file() else None,
                "music_folder": music if music and not music.is_file() else None,
                "music_seed": self.music_seed,
                "music_selection_mode": self.music_selection.currentData(),
                "beat_sync_enabled": preset in MUSIC_SHORTS_PRESETS,
                "beat_sync_mode": DEFAULT_BEAT_SYNC_MODE,
                "beat_sync_strength": DEFAULT_BEAT_SYNC_STRENGTH,
                "intro_image": (Path(self.intro_image.text()).resolve()
                                if self.intro_image.text().strip() else None),
                **self.presentation_values(),
            }
        except (OSError, ValueError) as exc:
            self.show_error(str(exc))
            return
        self.autosave_draft()

        def operation(cancel: Event, progress: Callable[[float], None],
                      stage: Callable[[str], None]) -> str:
            project = (load_project(root) if (root / "project.json").exists()
                       else create_project(root, paths))
            project = replace_sources(project, paths)
            values["webcam_rois"] = {
                source.id: webcams[source.path.resolve()] for source in project.sources
                if source.path.resolve() in webcams
            }
            values["shorts_square_rois"] = {
                source.id: squares[source.path.resolve()] for source in project.sources
                if source.path.resolve() in squares
            }
            settings = ProjectSettings.model_validate(project.settings.model_dump() | values)
            if cancel.is_set():
                raise Cancelled("Cancelled")
            save_project(root, project.model_copy(update={"settings": settings}))
            return str(root)

        def saved(result: str) -> None:
            self.root = Path(result)
            self.remember_project(self.root)
            self.job_succeeded = True
            self.status.setText(tr("Проект сохранён без экспорта."))

        self.job = Job(operation)
        self.job.result.connect(saved)
        self.job.failure.connect(self.show_error)
        self.job.finished.connect(self.job_finished)
        self.begin_job(tr("Сохранение проекта…"), detailed_feedback=False)

    def show_video_frame(self, frame: QVideoFrame) -> None:
        image = frame.toImage()
        if not image.isNull():
            self.video_frames_seen += 1
            self.video_frame_image = image
            self.fit_video_frame()
            if self.pause_on_frame:
                self.pause_on_frame = False
                self.player.pause()

    def clear_video_frame(self, _source: QUrl) -> None:
        self.video_frame_image = QImage()

    def fit_video_frame(self) -> None:
        if not self.video_frame_image.isNull():
            self.video.setPixmap(QPixmap.fromImage(self.video_frame_image).scaled(
                self.video.size(), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))

    def invalidate_episodes(self) -> None:
        self.seek.setRange(0, self.player.duration())
        if hasattr(self, "create_button"):
            self.create_button.setText(tr("Создать ролик"))

    def create_or_export(self) -> None:
        self.start_action("create")

    def output_source_time(self, position: int) -> int:
        if self.output_editor.isVisible():
            return self.output_editor.to_source(position)
        return position

    def seek_preview(self, position: int) -> None:
        self.episode_end = None
        self.player.setPosition(self.output_source_time(position))

    def seek_cut_preview(self, position: int) -> None:
        self.player.pause()
        self.seek_preview(position)

    def cut_view_changed(self, start: int, end: int) -> None:
        if self.output_editor.isVisible():
            self.seek.setRange(start, end)
            self.seek.setValue(self.output_editor.to_edited(self.player.position()))
            self.seek.popup.hide()

    def refresh_cut_preview(self) -> None:
        bar = self.output_editor.timeline
        self.cut_view_changed(
            round(bar.view_start * 1000), round((bar.view_start + bar.span) * 1000)
        )
        self.update_position(self.player.position())

    def set_seek_view(self, start: int, end: int) -> None:
        if end <= start:
            start, end = 0, self.player.duration()
        self.seek.setRange(start, end)
        self.seek.setValue(self.player.position())

    def update_seek_range(self, duration: int) -> None:
        if hasattr(self, "output_editor") and self.output_editor.isVisible():
            self.output_editor.set_duration(duration)
            self.refresh_cut_preview()
            return
        self.set_seek_view(0, duration)

    def seek_when_loaded(self, status: QMediaPlayer.MediaStatus) -> None:
        if self.pending_seek is not None and status in (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        ):
            position, self.pending_seek = self.pending_seek, None
            self.player.setPosition(position)

    def open_video(
        self, path: Path, title: str, autoplay: bool = False, start: int = 0, end: int | None = None
    ) -> None:
        context = {
            tr("Сохранённый вариант"): tr("ВАРИАНТ"),
            tr("Редактирование готового ролика"): tr("РЕДАКТИРОВАНИЕ"),
            tr("Правки сохранённого варианта"): tr("РЕДАКТИРОВАНИЕ"),
            tr("Готовый таймлапс"): tr("ГОТОВЫЙ РОЛИК"),
            tr("Готовый шортс"): tr("ГОТОВЫЙ ШОРТС"),
            tr("Предпросмотр шортса"): tr("ПРЕДПРОСМОТР"),
        }.get(title, title.upper())
        self.video_title.setText(path.name)
        self.video_title.setToolTip(str(path))
        self.viewer_context.setText(context)
        self.pause_on_frame = not autoplay
        self.pending_seek, self.episode_end = start, end
        url = QUrl.fromLocalFile(str(path.resolve()))
        if self.player.source() == url and self.player.duration() > 0:
            self.pending_seek = None
            self.player.setPosition(start)
        else:
            self.player.setSource(url)
        self.update_playback_controls()
        self.player.play()

    def update_playback_controls(self) -> None:
        active = not self.player.source().isEmpty() and self.job is None
        self.play_button.setEnabled(active)
        self.seek.setEnabled(active)

    def update_position(self, position: int) -> None:
        duration = self.player.duration()
        if self.output_editor.isVisible() and self.output_editor.duration:
            ranges = self.output_editor.ranges
            if position * 1000 >= ranges[-1][1]:
                self.player.pause()
            else:
                next_start = next((a for a, b in ranges if position * 1000 < b), None)
                if next_start is not None and position * 1000 < next_start:
                    self.player.setPosition((next_start + 999) // 1000)
                    return
            position = self.output_editor.to_edited(position)
            duration = self.output_editor.edited_duration // 1000
        if (
            self.pending_seek is None
            and self.episode_end is not None
            and position >= self.episode_end
        ):
            self.episode_end = None
            self.player.pause()
        if not self.seek.isSliderDown():
            self.seek.setValue(position)

        self.video_time.setText(f"{format_hms(position // 1000)} / {format_hms(duration // 1000)}")

    def choose_mode(self, mode: str) -> None:
        current = str(self.preset.currentData())
        shorts = current in SHORTS_PRESETS
        if (mode == "shorts") == shorts:
            self.sync_mode_controls()
            return
        target_preset = self.last_shorts_preset if mode == "shorts" else self.last_timelapse_preset
        index = self.preset.findData(target_preset)
        if index >= 0:
            self.preset.activate(index)

    def sync_mode_controls(self) -> None:
        preset = str(self.preset.currentData())
        shorts = preset in SHORTS_PRESETS
        self.mode_settings_title.setText(
            tr("Настройки Shorts") if shorts else tr("Настройки таймлапса")
        )
        if shorts:
            self.last_shorts_preset = preset
        else:
            self.last_timelapse_preset = preset
        self.timelapse_mode.setChecked(not shorts)
        self.shorts_mode.setChecked(shorts)
        self.area_stack.setCurrentIndex(1 if shorts else 0 if preset == "timelapse" else 2)
        self.square_layout_changed()
        self.preset.set_shorts_mode(shorts)
        self.duration_card.sync()

    def preset_changed(self) -> None:
        preset = self.preset.currentData()
        self.output_suffix.setText(output_suffix(str(preset)) + ".mp4")
        shorts = preset in SHORTS_PRESETS
        if shorts and not self.duration_in_seconds:
            self.normal_minutes = self.target.value()
            self.duration_in_seconds = True
            self.target.set_seconds_mode(True)
            self.target.setRange(0, 300)
            self.target.setDecimals(0)
            self.target.setSingleStep(5)
            self.target.setValue(self.shorts_seconds)
        elif not shorts and self.duration_in_seconds:
            self.shorts_seconds = self.target.value()
            self.duration_in_seconds = False
            self.target.set_seconds_mode(False)
            self.target.setDecimals(6)
            self.target.setRange(0, (2_147_483_647 * 3600 + 3599) / 60)
            self.target.setSingleStep(1)
            self.target.setValue(self.normal_minutes)
        self.sync_mode_controls()
        if hasattr(self, "seek"):
            self.invalidate_episodes()
        if hasattr(self, "change_music_button"):
            beat_mode = preset in MUSIC_SHORTS_PRESETS
            self.change_music_button.setText(
                tr("Сменить музыку и пересобрать") if beat_mode else tr("Сменить музыку")
            )
            self.update_result_actions()

    def frame_area_changed(self) -> None:
        self.update_aspect_captions()
        self.invalidate_episodes()

    def update_aspect_captions(self) -> None:
        ratio = 16 / 9
        paths = self.source_paths()
        if paths:
            cached = self.source_media_cache.get(str(self.selected_montage_source() or paths[0]))
            if cached is not None and not cached[0].isNull():
                ratio = cached[0].width() / cached[0].height()
        for value, aspect in (("full", ratio),
                              ("viewport", ratio * self.crop_roi.width / self.crop_roi.height)):
            fraction = Fraction(aspect).limit_denominator(20)
            caption = tr("Весь кадр") if value == "full" else tr("Рамка")
            self.frame_mode.buttons[value].setText(
                f"{caption} · {fraction.numerator}:{fraction.denominator}"
            )

    def choose_crop_area(self) -> None:
        paths = self.source_paths()
        if not paths:
            self.show_error("Add a source before setting crop")
            return
        source = self.selected_montage_source()
        if source is None:
            self.status.setText(tr("Для презентации нажмите ▶ на её карточке."))
            return
        try:
            cached = self.source_media_cache.get(str(source))
            dialog = TimelapseCropDialog(
                source, self.crop_roi, self, initial_frame=cached[0] if cached else None,
            )
        except ValueError as exc:
            self.show_error(str(exc))
            return
        result = dialog.exec()
        dialog.deleteLater()
        if result:
            before = self.history_controller.framing()
            self.crop_roi = dialog.roi
            self.crop_customized = True
            self.history_controller.record_framing(before)
            self.update_aspect_captions()
            self.invalidate_episodes()

    def square_layout_changed(self) -> None:
        if hasattr(self, "seek"):
            self.invalidate_episodes()

    def webcam_toggled(self, checked: bool) -> None:
        if hasattr(self, "seek"):
            self.invalidate_episodes()

    def configure_webcam(self, checked: bool) -> None:
        # A checked button still edits the area. Commit its active state only
        # after all dialogs are accepted; cancel preserves the previous setup.
        self.webcam_enabled.blockSignals(True)
        self.webcam_enabled.setChecked(not checked)
        self.webcam_enabled.blockSignals(False)
        self.choose_webcam_areas(force=True)

    def choose_webcam_areas(self, force: bool = False) -> bool:
        if self.job:
            return False
        paths = self.source_paths()
        if not paths:
            self.status.setText(tr("Сначала добавьте исходное видео."))
            return False
        selected = self.webcam_rois.copy()
        for path in paths:
            key = path.resolve()
            if key in selected and not force:
                continue
            cached = self.source_media_cache.get(str(path))
            dialog = WebcamDialog(path, selected.get(key), self,
                                  initial_frame=cached[0] if cached else None)
            result = dialog.exec()
            dialog.deleteLater()
            if result == QDialog.DialogCode.Accepted:
                selected[key] = dialog.roi
            elif result == 2:
                selected[key] = None
            else:
                return False
        before = self.history_controller.framing()
        self.webcam_rois = selected
        if force:
            self.webcam_enabled.blockSignals(True)
            self.webcam_enabled.setChecked(any(
                selected.get(path.resolve()) is not None for path in paths
            ))
            self.webcam_enabled.blockSignals(False)
        self.history_controller.record_framing(before)
        self.invalidate_episodes()
        return True

    def restore_square_layout(self, project: Project) -> None:
        self.webcam_rois = {
            source.path.resolve(): project.settings.webcam_rois[source.id]
            for source in project.sources if source.id in project.settings.webcam_rois
        }
        self.webcam_enabled.blockSignals(True)
        self.webcam_enabled.setChecked(project.settings.webcam_enabled)
        self.webcam_enabled.blockSignals(False)
        self.square_rois = {
            source.path.resolve(): project.settings.shorts_square_rois[source.id]
            for source in project.sources if source.id in project.settings.shorts_square_rois
        }
        self.shorts_layout.setCurrentIndex(
            self.shorts_layout.findData(project.settings.shorts_layout)
        )
        self.square_layout_changed()

    def choose_square_area(self, source: Path | None = None) -> bool:
        paths = self.source_paths()
        if not paths:
            self.show_error("Add a source before setting crop")
            return False
        source = source or self.selected_montage_source()
        if source is None:
            self.status.setText(tr("Для презентации нажмите ▶ на её карточке."))
            return False
        key = source.resolve()
        cached = self.source_media_cache.get(str(source))
        dialog = TimelapseCropDialog(
            source, self.square_rois.get(key, Roi(x=0, y=0, width=1, height=1)),
            self, square=True, initial_frame=cached[0] if cached else None,
        )
        result = dialog.exec()
        dialog.deleteLater()
        if result:
            before = self.history_controller.framing()
            self.square_rois[key] = dialog.roi
            self.history_controller.record_framing(before)
            self.invalidate_episodes()
            return True
        return False

    def button(self, text: str, callback: Callable[[], None]) -> QPushButton:
        button = QPushButton(text)
        button.clicked.connect(callback)
        self.controls.append(button)
        return button

    def choose_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, tr("Папка проекта"))
        if chosen:
            self.open_project_folder(Path(chosen))

    def new_project(self) -> None:
        if self.job:
            return
        from sculptcut.new_project_dialog import NewProjectDialog
        from sculptcut.project import create_empty_project

        documents = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DocumentsLocation,
        )
        default = Path(documents) if documents else Path.home()
        if self.folder.text().strip():
            default = Path(self.folder.text().strip()).absolute().parent
        if not default.is_dir():
            default = Path.home()
        dialog = NewProjectDialog(default, self)
        result = exec_anchored(dialog, self.new_project_button)
        target = dialog.target
        dialog.deleteLater()
        if result != QDialog.DialogCode.Accepted or target is None:
            return

        def operation(cancel: Event, progress: Callable[[float], None],
                      stage: Callable[[str], None]) -> str:
            if cancel.is_set():
                raise Cancelled("Cancelled")
            create_empty_project(target)
            return str(target)

        def created(result: str) -> None:
            if self.episode_panel is not None:
                self.episode_panel.close()
                self.episode_panel = None
            self.output_editor.hide()
            self.output_editor.reset()
            self.set_output_editor_button(False)
            self.open_project_folder(Path(result))
            self.shorts_seconds = 60.0
            self.last_timelapse_preset = "timelapse"
            self.last_shorts_preset = "shorts"
            self.refresh_variants()
            self.job_succeeded = True
            self.status.setText(tr("Проект создан. Добавьте исходные видео."))
            self.autosave_draft()

        self.job = Job(operation)
        self.job.result.connect(created)
        self.job.failure.connect(self.show_error)
        self.job.finished.connect(self.job_finished)
        self.begin_job(tr("Создание проекта…"), detailed_feedback=False)

    def recent_project_paths(self) -> list[Path]:
        saved = self.recent_settings.value("recent_projects", [])
        values = [saved] if isinstance(saved, str) else saved if isinstance(saved, list) else []
        return [Path(value) for value in values if isinstance(value, str) and value]

    def remember_project(self, root: Path) -> None:
        if not (root / "project.json").is_file():
            return
        current = root.resolve()
        paths = [current]
        paths.extend(
            path
            for path in self.recent_project_paths()
            if str(path).casefold() != str(current).casefold()
        )
        self.recent_settings.setValue("recent_projects", [str(path) for path in paths[:10]])
        self.recent_settings.sync()

    def show_recent_projects(self) -> None:
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        paths = self.recent_project_paths()
        if not paths:
            menu.addAction(tr("Пока нет недавних проектов")).setEnabled(False)
        for root in paths:
            available = (root / "project.json").is_file()
            label = f"{root.name}  ·  {root.parent}"
            action = menu.addAction(label if available else label + tr(" (недоступен)"))
            action.setData(str(root))
            action.setToolTip(str(root))
            action.setEnabled(available)
        chosen = menu.exec(self.recent_button.mapToGlobal(QPoint(0, self.recent_button.height())))
        if chosen is not None and chosen.data():
            self.open_project_folder(Path(str(chosen.data())))

    def open_project_folder(self, root: Path) -> None:
        self.edit_history.clear()
        root = root.resolve()
        if not (root / "project.json").is_file():
            self.clear_final_view()
            self.root = None
            if self.folder.text().strip() != str(root):
                self.output_name.clear()
            self.folder.setText(str(root))
            return
        try:
            project = load_project(root)
            from sculptcut.export_storage import organize_exports

            organize_exports(root)
            self.clear_final_view()
            self.folder.setText(str(root))
            self.root = root
            self.set_sources([s.path for s in project.sources])
            if project.settings.preset in SHORTS_PRESETS:
                self.shorts_seconds = project.settings.target_seconds
            else:
                self.normal_minutes = project.settings.target_seconds / 60
            self.preset.blockSignals(True)
            self.preset.setCurrentIndex(self.preset.findData(project.settings.preset))
            self.preset.blockSignals(False)
            self.preset_changed()
            self.frame_mode.blockSignals(True)
            self.frame_mode.setCurrentIndex(
                self.frame_mode.findData(
                    "full" if project.settings.crop_mode == "full" else "viewport"
                )
            )
            self.frame_mode.blockSignals(False)
            self.restore_square_layout(project)
            self.crop_roi = project.settings.timelapse_viewport_roi
            self.crop_customized = project.settings.crop_mode == "viewport"
            self.target.setValue(
                project.settings.target_seconds
                if self.duration_in_seconds
                else project.settings.target_seconds / 60
            )
            self.music.setText(
                str(project.settings.music_folder or project.settings.music_path or ""),
            )
            self.music_selection.setCurrentIndex(
                self.music_selection.findData(project.settings.music_selection_mode)
            )
            self.intro_image.setText(str(project.settings.intro_image or ""))
            self.restore_presentation(project.settings)
            self.output_name.setText(project.settings.output_name)
            self.music_seed = project.settings.music_seed
            for directory, title in (
                ("renders", tr("Готовый таймлапс")),
                ("previews", tr("Сохранённый предпросмотр")),
            ):
                videos = ordered_exports(root / directory)
                if videos:
                    latest = videos[0]
                    self.last_result_video = latest
                    self.open_video(latest, title)
                    break
            self.update_result_actions()
            self.remember_project(root)
            self.find_missing_sources()
        except (OSError, ValueError) as exc:
            self.show_error(str(exc))

    def find_missing_clicked(self) -> None:
        self.find_missing_sources()

    def find_missing_sources(self, after: Callable[[], None] | None = None) -> bool:
        """Pick candidates on the GUI thread; verify and persist them in a worker."""
        if self.job or not self.folder.text().strip():
            return False
        root = Path(self.folder.text()).resolve()
        if not (root / "project.json").is_file():
            self.relink_button.hide()
            return False
        try:
            project = load_project(root)
        except (OSError, ValueError) as exc:
            self.show_error(str(exc))
            return True
        active = self.source_paths()
        missing = [s for s in project.sources if s.path.resolve() in active
                   and not s.path.is_file()]
        self.relink_button.setVisible(bool(missing))
        if not missing:
            return False
        replacements: dict[str, Path] = {}
        for source in missing:
            selected, _ = QFileDialog.getOpenFileName(
                self, tr(f"Найти исходник: {source.path.name}"),
                str(source.path.parent if source.path.parent.is_dir() else root),
                tr("Видео (*.mp4 *.mkv *.mov *.webm *.avi);;Все файлы (*)"),
            )
            if not selected:
                self.status.setText(
                    tr("Исходники не найдены. Используйте «Найти исходники…», чтобы продолжить.")
                )
                return True
            replacements[source.id] = Path(selected)

        def operation(cancel: Event, progress: Callable[[float], None],
                      stage: Callable[[str], None]) -> str:
            relink_sources(root, replacements, cancel)
            return str(root)

        def linked(result: str) -> None:
            updated = load_project(Path(result))
            locations = {old.path.resolve(): new.path for old, new
                         in zip(project.sources, updated.sources, strict=True)}
            self.webcam_rois = {locations.get(path, path): roi
                                for path, roi in self.webcam_rois.items()}
            self.square_rois = {locations.get(path, path): roi
                                for path, roi in self.square_rois.items()}
            self.set_sources([locations.get(path, path) for path in self.listed_source_paths()])
            self.job_succeeded = True
            self.relink_button.hide()
            self.status.setText(tr("Исходники найдены и проверены. Монтаж и области сохранены."))
            self.autosave_draft()

        self.job = Job(operation)
        self.job.result.connect(linked)
        self.job.failure.connect(self.show_error)
        self.job.finished.connect(self.job_finished)
        if after is not None:
            self.job.finished.connect(
                lambda: QTimer.singleShot(0, after) if self.job_succeeded else None
            )
        self.begin_job(tr("Проверка перемещённых исходников…"))
        return True

    def clear_final_view(self) -> None:
        """Reset only the output preview when changing projects."""
        self.player.stop()
        self.player.setSource(QUrl())
        self.video.clear()
        self.video.setText(tr(READY_VIDEO_HTML if self.sources.count() else EMPTY_VIDEO_HTML))
        self.video_title.setText(tr("Готовый ролик"))
        self.video_title.setToolTip("")
        self.viewer_context.setText(tr("ЕЩЁ НЕ СОЗДАН"))
        self.video_time.setText("00:00 / 00:00")
        self.last_result_video = None
        self.update_playback_controls()

    def set_sources(self, paths: list[Path]) -> None:
        unique = list(dict.fromkeys(path.resolve() for path in paths))
        if unique != self.listed_source_paths():
            self.edit_history.clear()
        kept = {str(path) for path in unique}
        self.source_media_cache = {
            key: value for key, value in self.source_media_cache.items() if key in kept
        }
        self.sources.clear()
        pending: list[Path] = []
        for path in unique:
            item = QListWidgetItem(path.name)
            item.setSizeHint(QSize(0, 82))
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            item.setToolTip(str(path))
            cached = self.source_media_cache.get(str(path))
            if cached is not None:
                self.apply_source_media(item, *cached)
            else:
                item.setData(META_ROLE, tr("Сведения загружаются…"))
                if str(path) not in self.source_media_pending:
                    self.source_media_pending.add(str(path))
                    pending.append(path)
            self.sources.addItem(item)
            self.update_source_role(item)
        if unique:
            self.sources.setCurrentRow(0)
        load_source_media(pending, self.source_media_loaded)

    def apply_source_media(self, item: QListWidgetItem, frame: QImage, detail: str) -> None:
        item.setData(META_ROLE, detail)
        if not frame.isNull():
            image = QPixmap.fromImage(frame).scaled(
                96,
                58,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            item.setIcon(QIcon(image))

    def source_media_loaded(self, path: str, frame: QImage, detail: str) -> None:
        self.source_media_pending.discard(path)
        for index in range(self.sources.count()):
            item = self.sources.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == path:
                self.source_media_cache[path] = (frame, detail)
                self.apply_source_media(item, frame, detail)
                self.source_preview_ready.emit(path, frame, detail)
                self.update_aspect_captions()
                break

    def add_dropped_videos(self, paths: list[Path]) -> None:
        if self.job or not paths:
            return
        previous = self.listed_source_paths()
        self.set_sources(previous + paths)
        if not self.folder.text().strip() and not self.offer_new_project():
            self.set_sources(previous)
            if previous:
                self.status.setText(
                    tr("Новые видео не добавлены. Для оставшихся выберите папку проекта.")
                )
                self.folder.setFocus()
            self.autosave_draft()

    def offer_new_project(self) -> bool:
        paths = self.source_paths()
        if self.job or not paths:
            return False
        if self.folder.text().strip():
            return True
        chosen = QFileDialog.getExistingDirectory(
            self, tr("Где создать новый проект?"), str(paths[0].parent),
        )
        if not chosen:
            self.status.setText(tr("Добавление видео отменено: проект не создан."))
            return False
        parent = Path(chosen).resolve()
        if not parent.is_dir():
            self.show_error(tr("Выберите существующую папку для нового проекта."))
            return False
        name = (paths[0].stem[:80].rstrip(" .") or tr("Новый проект")) + "_ArtLapse"
        root = parent / name
        suffix = 2
        while root.exists():
            root = parent / f"{name}-{suffix:03d}"
            suffix += 1
        self.folder.setText(str(root))
        self.save_current_project()
        return True

    def update_source_summary(self) -> None:
        count = self.sources.count()
        self.presentation_button.setEnabled(self.job is None and count > 0)
        self.source_stack.setCurrentWidget(self.sources if count else self.drop_zone)
        if self.player.source().isEmpty():
            self.video.setText(tr(READY_VIDEO_HTML if count else EMPTY_VIDEO_HTML))
        self.update_result_actions()

    def update_result_actions(self) -> None:
        self.presentation_button.setEnabled(self.job is None and self.sources.count() > 0)
        self.remove_button.setEnabled(self.job is None and bool(self.sources.selectedItems()))
        project = Path(self.folder.text().strip()) if self.folder.text().strip() else None
        self.cleanup_button.setEnabled(
            self.job is None and project is not None and (project / "project.json").is_file()
        )
        has_output = bool(project and has_exports(project / "renders"))
        self.episodes_button.setEnabled(
            self.job is None and project is not None and (project / "render_plan.json").is_file()
        )
        self.change_music_button.setEnabled(has_output and self.job is None)
        self.change_intro_button.setEnabled(has_output and self.job is None)
        self.outputs_button.setEnabled(has_output)
        if not has_output:
            hint = tr("Доступно после создания первого ролика")
            self.change_music_button.setToolTip(hint)
            self.change_intro_button.setToolTip(hint)
            self.outputs_button.setToolTip(hint)
        else:
            self.change_music_button.setToolTip(
                tr("Новая музыка пересоберёт склейки; у подрезанной версии видеоряд сохранится")
                if self.preset.currentData() in MUSIC_SHORTS_PRESETS
                else tr("Выберите музыку справа и создайте новую версию ролика")
            )
            self.change_intro_button.setToolTip(
                tr("Выберите изображение справа и создайте новую версию ролика")
            )
            self.outputs_button.setToolTip(tr("Открыть папку с уже созданными MP4"))
        if hasattr(self, "cut_button"):
            self.cut_button.setVisible(has_output)
            self.cut_section.setVisible(has_output)
        self.update_intro_controls()

    def listed_source_paths(self) -> list[Path]:
        return [
            Path(str(self.sources.item(i).data(Qt.ItemDataRole.UserRole)))
            for i in range(self.sources.count())
        ]

    def source_paths(self) -> list[Path]:
        presentation = self.presentation_video.resolve() if self.presentation_video else None
        return [path for path in self.listed_source_paths() if path != presentation]

    def selected_montage_source(self) -> Path | None:
        paths = self.source_paths()
        item = self.sources.currentItem()
        if item is not None:
            path = Path(str(item.data(Qt.ItemDataRole.UserRole)))
            if path in paths:
                return path
        # Global framing controls still target the montage when its presentation is selected.
        return next(iter(paths), None)

    def update_source_role(self, item: QListWidgetItem) -> None:
        path = Path(str(item.data(Qt.ItemDataRole.UserRole)))
        active = self.presentation_video is not None and path == self.presentation_video.resolve()
        place = tr("В начале") if self.presentation_position == "start" else tr("В конце")
        caption = tr(f"{place} · {self.presentation_seconds:g} с") if active else ""
        item.setData(PRESENTATION_ROLE, caption)
        item.setData(Qt.ItemDataRole.AccessibleDescriptionRole,
                     tr("Презентация · ") + caption if active else tr("Для монтажа"))
        item.setToolTip(str(path) + (tr("\nПрезентация · ") + caption if active else ""))

    def clean_project(self) -> None:
        if self.job or not self.folder.text().strip():
            return
        from sculptcut.cleanup_dialog import CleanupDialog
        from sculptcut.project_cleanup import cleanup_files, format_bytes, remove_cleanup_files
        from sculptcut.project_deletion import remove_project_folder

        root = Path(self.folder.text().strip()).absolute()
        inputs = self.source_paths() + [
            Path(field.text().strip()) for field in (self.music, self.intro_image)
            if field.text().strip()
        ]
        if self.presentation_video is not None:
            inputs.append(self.presentation_video)
        try:
            files = cleanup_files(root, inputs)
        except (OSError, ValueError) as exc:
            self.show_error(str(exc))
            return
        dialog = CleanupDialog(root, files, self, extra_inputs=inputs)
        result = exec_anchored(dialog, self.cleanup_button)
        dialog.deleteLater()
        if result != QDialog.DialogCode.Accepted:
            return
        selected = dialog.selected_files()
        whole = dialog.delete_project.isChecked()
        target = dialog.project_target
        if (not whole and not selected) or (whole and target is None):
            return
        if whole and self.episode_panel is not None and self.episode_panel.root == root:
            self.episode_panel.close()
            self.episode_panel = None
        paths = {entry.path for entry in selected}
        current = self.player.source().toLocalFile()
        if whole or (current and Path(current).resolve() in paths):
            self.output_editor.hide()
            self.output_editor.reset()
            self.set_output_editor_button(False)
            self.clear_final_view()
            self.edit_history.clear()
        if whole or (self.last_result_video and self.last_result_video.resolve() in paths):
            self.last_result_video = None

        def operation(
            cancel: Event, progress: Callable[[float], None], stage: Callable[[str], None],
        ) -> str:
            stage(
                tr("Проверка и удаление папки проекта…")
                if whole
                else tr("Удаление выбранных файлов…")
            )
            result = (remove_project_folder(target, cancel, progress, inputs)
                      if whole and target is not None else
                      remove_cleanup_files(root, selected, cancel, progress, inputs))
            return json.dumps({
                "removed": result.removed, "bytes": result.bytes_removed,
                "errors": result.errors, "cancelled": result.cancelled,
                "project_removed": whole and not root.exists() and not result.errors,
            })

        def cleaned(value: str) -> None:
            result = json.loads(value)
            self.job_succeeded = not result["errors"] and not result["cancelled"]
            if result["project_removed"]:
                paths = [str(path) for path in self.recent_project_paths()
                         if path.absolute() != root]
                self.recent_settings.setValue("recent_projects", paths)
                self.folder.clear()
                self.root = None
                self.set_sources([])
                self.square_rois.clear()
                self.webcam_rois.clear()
                self.restore_presentation(ProjectSettings())
                self.output_name.clear()
                self.clear_final_view()
                self.autosave_draft()
            self.refresh_variants()
            self.update_result_actions()
            prefix = (tr("Проект удалён.") if result["project_removed"] else
                      tr("Очистка остановлена.") if result["cancelled"] or result["errors"] else
                      tr("Очистка завершена."))
            self.status.setText(
                tr(f"{prefix} Удалено файлов: {result['removed']}, "
                f"освобождено {format_bytes(result['bytes'])}.")
            )
            if result["errors"]:
                self.status.setText(
                    self.status.text() + tr(f" Не удалено: {len(result['errors'])}.")
                )
                details = "\n".join(result["errors"][:10])
                if len(result["errors"]) > 10:
                    details += tr(f"\n…и ещё {len(result['errors']) - 10}.")
                warning = QMessageBox(
                    QMessageBox.Icon.Warning, tr("Не все файлы удалены"),
                    self.status.text() + "\n\n" + details,
                    QMessageBox.StandardButton.Ok, self,
                )
                style_message_box(warning)
                warning.exec()
                warning.deleteLater()

        self.job = Job(operation)
        self.job.result.connect(cleaned)
        self.job.failure.connect(self.show_error)
        self.job.progress.connect(self.show_progress)
        self.job.stage.connect(self.show_stage)
        self.job.finished.connect(self.job_finished)
        self.begin_job(tr("Очистка проекта…"))

    def remove_sources(self) -> None:
        if self.job:
            return
        rows = sorted(
            (self.sources.row(item) for item in self.sources.selectedItems()), reverse=True
        )
        if not rows:
            return
        self.sources.blockSignals(True)
        for row in rows:
            self.sources.takeItem(row)
        self.sources.blockSignals(False)
        if self.presentation_video is not None and (
            self.presentation_video.resolve() not in self.listed_source_paths()
        ):
            self.restore_presentation(ProjectSettings())
        self.update_source_summary()
        if self.sources.count():
            self.sources.setCurrentRow(min(rows[-1], self.sources.count() - 1))
        self.status.setText(
            tr("Убрано из списка: ") + str(len(rows)) + tr(". Исходные файлы сохранены.")
        )

    def open_outputs(self) -> None:
        text = self.folder.text().strip()
        directory = Path(text).resolve() / "renders" if text else None
        if directory is None or not directory.is_dir():
            self.status.setText(tr("Готовых видео пока нет. Выберите проект и выполните экспорт."))
            return
        try:
            from sculptcut.export_storage import organize_exports

            organize_exports(directory.parent)
        except (OSError, ValueError) as exc:
            self.show_error(tr(f"Не удалось упорядочить готовые видео: {exc}"))
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory))):
            self.status.setText(tr("Не удалось открыть папку: ") + str(directory))

    def choose_music_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, tr("Выберите папку с музыкой"))
        if path:
            self.music.setText(path)
            self.shuffle_music()

    def choose_music_file(self) -> None:
        extensions = " ".join("*" + suffix for suffix in sorted(EXTENSIONS))
        path, _ = QFileDialog.getOpenFileName(
            self, tr("Выберите музыкальный файл"), self.music.text(),
            tr("Музыка") + f" ({extensions});;" + tr("Все файлы (*)"),
        )
        if path:
            self.music.setText(path)
            self.status.setText(tr("Музыкальный файл выбран."))

    def presentation_values(self) -> dict[str, object]:
        paths = self.listed_source_paths()
        return {
            "presentation_video": self.presentation_video,
            "presentation_seconds": self.presentation_seconds,
            "presentation_position": self.presentation_position,
            "presentation_source_index": (paths.index(self.presentation_video)
                                          if self.presentation_video in paths else 0),
        }

    def restore_presentation(self, settings: ProjectSettings) -> None:
        self.presentation_video = settings.presentation_video
        self.presentation_seconds = settings.presentation_seconds
        self.presentation_position = settings.presentation_position
        paths = self.listed_source_paths()
        if self.presentation_video is not None and self.presentation_video.resolve() not in paths:
            paths.insert(min(settings.presentation_source_index, len(paths)),
                         self.presentation_video.resolve())
            self.set_sources(paths)
        for index in range(self.sources.count()):
            self.update_source_role(self.sources.item(index))
        self.sources.viewport().update()
        self.update_presentation_button()
        self.update_source_summary()
        self.update_aspect_captions()

    def update_presentation_button(self) -> None:
        active = self.presentation_video is not None
        self.presentation_button.setIcon(ui_icon(
            "presentation", "#efc681" if active else "#a6bfdc",
        ))
        self.presentation_button.setToolTip(
            tr(f"Презентация проекта: {self.presentation_video.name}\nИзменить ролик или параметры")
            if self.presentation_video else tr("Выбрать единственный презентационный ролик проекта")
        )
        self.presentation_button.setStyleSheet(
            "QPushButton { padding:5px; border-radius:8px; "
            + ("background:#362f25; border:1px solid #8e7047;" if active
               else "background:transparent; border:1px solid transparent;")
            + "} QPushButton:hover { background:#314155; border:1px solid #647f9b; }"
            " QPushButton:focus { border:1px solid #8cbcff; }"
        )
        self.update_intro_controls()

    def update_intro_controls(self) -> None:
        if not hasattr(self, "intro_label"):
            return
        active = self.presentation_video is not None
        hint = tr(
            "Выбрано презентационное видео. Отключите презентацию, чтобы использовать заставку."
        )
        self.intro_label.setText(tr("Заставка · не используется") if active else tr("Заставка"))
        self.intro_label.setToolTip(hint if active else "")
        self.intro_image.setEnabled(not active and self.job is None)
        self.intro_preview.setEnabled(not active)
        self.intro_image.setPlaceholderText(
            tr("Используется презентационное видео") if active
            else tr("Нажмите, чтобы выбрать PNG/JPG")
        )
        self.intro_image.setToolTip(
            hint if active else tr("Нажмите на поле, чтобы выбрать другую заставку; × — очистить")
        )
        if active and hasattr(self, "change_intro_button"):
            self.change_intro_button.setEnabled(False)
            self.change_intro_button.setToolTip(hint)

    def choose_presentation(self) -> None:
        if self.job:
            return
        from sculptcut.presentation_dialog import PresentationDialog

        paths = self.listed_source_paths()
        if not paths:
            return
        dialog = PresentationDialog(self.presentation_video, self.presentation_seconds,
                                    self.presentation_position, paths, self,
                                    media=self.source_media_cache)
        self.source_preview_ready.connect(dialog.set_source_media)
        result = exec_anchored(dialog, self.presentation_button)
        if result == QDialog.DialogCode.Accepted:
            settings = ProjectSettings(
                presentation_video=dialog.path, presentation_seconds=dialog.seconds.value(),
                presentation_position=dialog.placement,
                presentation_source_index=paths.index(dialog.path) if dialog.path else 0,
            )
            self.restore_presentation(settings)
            self.edit_history.clear()
            self.invalidate_episodes()
            self.autosave_draft()
            self.status.setText(
                tr("Презентация проекта настроена для всех режимов.")
                if settings.presentation_video else tr("Презентация проекта отключена.")
            )
        dialog.deleteLater()

    def choose_intro_image(self) -> None:
        if self.presentation_video is not None:
            self.status.setText(tr(
                "Выбрано презентационное видео. Отключите презентацию, чтобы использовать заставку."
            ))
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("Выберите изображение заставки"),
            "",
            tr("Изображения (*.png *.jpg *.jpeg *.webp *.bmp)"),
        )
        if not path:
            return
        image = QPixmap(path)
        if image.isNull():
            self.show_error(tr("Не удалось прочитать изображение заставки: ") + path)
            return
        has_video = not self.player.source().isEmpty()
        if has_video:
            self.player.pause()
        self.intro_image.setText(str(Path(path).resolve()))
        if self.change_intro_button.isEnabled():
            message = tr("Новая заставка выбрана. Нажмите «Обновить заставку».")
            self.progress_details.setText(tr("Готово к обновлению заставки"))
        else:
            message = tr("Заставка выбрана. Она будет добавлена при создании ролика.")
            self.progress_details.setText(tr("Заставка выбрана · обработка не запущена"))
        if has_video:
            message += tr(" Видео поставлено на паузу; «Просмотр» продолжит воспроизведение.")
        self.status.setText(message)

    def change_music_from_button(self) -> None:
        folder = Path(self.music.text().strip()) if self.music.text().strip() else None
        if folder is None or not folder.exists():
            self.choose_music_folder()
            folder = Path(self.music.text().strip()) if self.music.text().strip() else None
        if folder is not None and (folder.is_dir() or folder.is_file()):
            self.another_music()

    def change_intro_from_button(self) -> None:
        if self.presentation_video is not None:
            self.choose_intro_image()
            return
        image = Path(self.intro_image.text().strip()) if self.intro_image.text().strip() else None
        if image is None or not image.is_file():
            self.choose_intro_image()
            image = (
                Path(self.intro_image.text().strip()) if self.intro_image.text().strip() else None
            )
        if image is not None and image.is_file():
            self.another_intro()

    def another_music(self) -> None:
        if self.job:
            return
        folder = Path(self.music.text().strip()) if self.music.text().strip() else None
        if folder is None or not (folder.is_dir() or folder.is_file()):
            self.status.setText(tr("Выберите музыкальный файл или папку для другого варианта."))
            return
        root = Path(self.folder.text().strip()).resolve()
        videos = [p for name in ("renders", "previews") for p in (root / name).glob("*.mp4")]
        if not videos:
            self.shuffle_music()
            return
        latest = getattr(self, "last_result_video", None)
        video = latest if latest in videos else max(videos, key=lambda p: p.stat().st_mtime_ns)
        try:
            state = read_variant(video)
            snapshot = state.get("project")
            preset = (Project.model_validate(snapshot).settings.preset if snapshot
                      else load_project(root).settings.preset)
        except (OSError, ValueError) as exc:
            self.show_error(str(exc))
            return
        from sculptcut.variants import has_manual_edits

        try:
            trimmed = has_manual_edits(video)
        except (OSError, ValueError) as exc:
            self.show_error(str(exc))
            return
        if preset in MUSIC_SHORTS_PRESETS and not trimmed:
            if self.find_missing_sources(self.another_music):
                return
        seed = secrets.randbits(32)
        music_selection_mode = cast(
            Literal["dynamic", "full_tracks"], self.music_selection.currentData(),
        )
        self.player.stop()
        self.export_note = tr(" Музыка обновлена; предыдущая версия сохранена.")

        def operation(
            cancel: Event, progress: Callable[[float], None], stage: Callable[[str], None]
        ) -> str:
            from sculptcut.remusic import replace_music

            stage(
                tr("Меняем музыку; видеоряд и вырезки сохранены") if trimmed
                else tr("Подбираем музыку; музыкальные кадры пересобираются по ритму")
            )
            return str(
                replace_music(
                    root, video, folder.resolve(), seed, cancel, music_selection_mode, progress,
                )
            )

        self.music_seed = seed
        self.job = Job(operation)
        self.job.result.connect(self.show_result)
        self.job.failure.connect(self.show_error)
        self.job.progress.connect(self.show_progress)
        self.job.stage.connect(self.show_stage)
        self.job.finished.connect(self.job_finished)
        self.begin_job(tr("Подбираем другую музыку для выбранной версии…"))

    def another_intro(self) -> None:
        if self.job:
            return
        if self.presentation_video is not None:
            self.choose_intro_image()
            return
        if self.find_missing_sources(self.another_intro):
            return
        image = Path(self.intro_image.text().strip()).resolve()
        if not image.is_file() or QPixmap(str(image)).isNull():
            self.status.setText(tr("Выберите новое изображение заставки."))
            return
        root = Path(self.folder.text().strip()).resolve()
        videos = [p for p in (root / "renders").glob("*.mp4") if p.is_file()]
        if not videos:
            self.status.setText(tr("Сначала создайте готовый ролик."))
            return
        latest = getattr(self, "last_result_video", None)
        video = latest if latest in videos else max(videos, key=lambda p: p.stat().st_mtime_ns)
        self.player.stop()
        self.export_note = tr(" Заставка обновлена; монтаж и музыка сохранены.")

        def operation(
            cancel: Event, progress: Callable[[float], None], stage: Callable[[str], None]
        ) -> str:
            from sculptcut.reintro import replace_intro

            stage(tr("Обновляем заставку без повторного анализа и монтажа"))
            return str(replace_intro(root, video, image, cancel, progress))

        self.job = Job(operation)
        self.job.result.connect(self.show_result)
        self.job.failure.connect(self.show_error)
        self.job.progress.connect(self.show_progress)
        self.job.stage.connect(self.show_stage)
        self.job.finished.connect(self.job_finished)
        self.begin_job(tr("Обновляем заставку… Анализ и монтаж не запускаются."))

    def music_path_changed(self, text: str) -> None:
        self.update_result_actions()
        if not text:
            self.status.setText(tr("Музыка отключена."))

    def intro_path_changed(self, text: str) -> None:
        self.update_result_actions()
        image = QPixmap(text) if text else QPixmap()
        self.intro_preview.setVisible(not image.isNull())
        if not image.isNull():
            self.intro_preview.setPixmap(
                image.scaled(
                    52,
                    34,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            self.intro_preview.clear()
        if text:
            return
        self.progress_details.setText(tr("Заставка отключена · обработка не запущена"))
        self.status.setText(tr("Заставка отключена."))

    def reset_output_editor(self, url: QUrl) -> None:
        self.output_editor.hide()
        self.output_editor.reset()
        self.set_output_editor_button(False)

    def refresh_variants(self) -> None:
        selected = getattr(self, "last_result_video", None)
        self.variants.blockSignals(True)
        self.variants.clear()
        root = Path(self.folder.text().strip())
        if self.folder.text().strip():
            for path in ordered_exports(root / "renders"):
                self.variants.addItem(path.name, str(path))
            if selected:
                index = self.variants.findData(str(selected.resolve()))
                if index >= 0:
                    self.variants.setCurrentIndex(index)
            if self.variants.currentIndex() < 0 and self.variants.count():
                self.variants.setCurrentIndex(0)
        self.variants.blockSignals(False)
        self.update_variant_tooltip()
        self.versions_panel.setVisible(self.variants.count() > 0)

    def update_variant_tooltip(self) -> None:
        self.variants.setToolTip(str(self.variants.currentData() or tr("Выберите готовый ролик")))
        self.delete_variant_button.setEnabled(
            self.job is None and bool(self.variants.currentData())
        )

    def delete_selected_variant(self) -> None:
        if self.job is not None or not self.variants.currentData():
            return
        from sculptcut.variant_deletion import delete_variant, variant_files

        root = Path(self.folder.text().strip()).resolve()
        video = Path(self.variants.currentData()).absolute()
        inputs = self.source_paths() + [
            Path(field.text().strip()) for field in (self.music, self.intro_image)
            if field.text().strip()
        ]
        if self.presentation_video is not None:
            inputs.append(self.presentation_video)
        try:
            files = variant_files(root, video, inputs)
        except (OSError, ValueError) as exc:
            self.show_error(str(exc))
            return
        dialog = QMessageBox(QMessageBox.Icon.Warning, tr("Удалить выбранную версию?"),
                             video.name, QMessageBox.StandardButton.Cancel, self)
        dialog.setObjectName("deleteVariantConfirmation")
        dialog.setInformativeText(tr(
            "MP4 и история этой версии будут удалены с диска. "
            "Исходники и другие версии останутся."
        ))
        remove = dialog.addButton(tr("Удалить"), QMessageBox.ButtonRole.DestructiveRole)
        dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
        style_message_box(dialog)
        dialog.exec()
        confirmed = dialog.clickedButton() == remove
        dialog.deleteLater()
        if not confirmed:
            return
        current = self.player.source().toLocalFile()
        was_current = bool(current and Path(current).resolve() == video)
        if was_current:
            self.clear_final_view()  # Release the MP4 handle before unlinking on Windows.
            self.edit_history.clear()

        def operation(cancel: Event, progress: Callable[[float], None],
                      stage: Callable[[str], None]) -> str:
            stage(tr("Удаление выбранной версии…"))
            result = delete_variant(root, video, files, cancel, progress, inputs)
            return json.dumps({"errors": result.errors, "cancelled": result.cancelled})

        def refresh() -> None:
            self.refresh_variants()
            if was_current and self.variants.count():
                self.open_variant(self.variants.currentIndex())
            self.update_result_actions()

        def deleted(value: str) -> None:
            result = json.loads(value)
            self.job_succeeded = not result["errors"] and not result["cancelled"]
            refresh()
            if result["errors"]:
                self.show_error("\n".join(result["errors"]))
            else:
                self.status.setText(tr("Удаление отменено.") if result["cancelled"]
                                    else tr("Выбранная версия удалена."))

        def failed(message: str) -> None:
            refresh()
            self.show_error(message)

        self.job = Job(operation)
        self.job.result.connect(deleted)
        self.job.failure.connect(failed)
        self.job.progress.connect(self.show_progress)
        self.job.stage.connect(self.show_stage)
        self.job.finished.connect(self.job_finished)
        self.begin_job(tr("Удаление выбранной версии…"), detailed_feedback=False)

    def open_variant(self, index: int) -> None:
        path = Path(self.variants.itemData(index))
        self.last_result_video = path
        self.open_video(path, tr("Сохранённый вариант"))

    def restore_variant(self) -> None:
        self.edit_history.clear()
        if not self.variants.currentData():
            return
        try:
            state = read_variant(Path(self.variants.currentData()))
            if not state.get("project"):
                self.status.setText(
                    tr("У старого ролика нет снимка настроек. Можно смотреть и подрезать.")
                )
                return
            project = Project.model_validate(state["project"])
            import json

            from sculptcut.project import atomic_write

            root = Path(self.folder.text())
            project = resolve_snapshot_sources(project, load_project(root))
            save_project(root, project)
            if state.get("plan"):
                atomic_write(
                    root / "render_plan.json", json.dumps(state["plan"], ensure_ascii=False)
                )
            self.set_sources([s.path for s in project.sources])
            if project.settings.preset in SHORTS_PRESETS:
                self.shorts_seconds = project.settings.target_seconds
            else:
                self.normal_minutes = project.settings.target_seconds / 60
            self.preset.blockSignals(True)
            self.preset.setCurrentIndex(self.preset.findData(project.settings.preset))
            self.preset.blockSignals(False)
            self.preset_changed()
            self.frame_mode.blockSignals(True)
            self.frame_mode.setCurrentIndex(
                self.frame_mode.findData(
                    "full" if project.settings.crop_mode == "full" else "viewport"
                )
            )
            self.frame_mode.blockSignals(False)
            self.restore_square_layout(project)
            self.crop_roi = project.settings.timelapse_viewport_roi
            self.crop_customized = project.settings.crop_mode == "viewport"
            self.target.setValue(
                project.settings.target_seconds
                if self.duration_in_seconds
                else project.settings.target_seconds / 60
            )
            self.music.setText(
                str(project.settings.music_folder or project.settings.music_path or ""),
            )
            self.music_selection.setCurrentIndex(
                self.music_selection.findData(project.settings.music_selection_mode)
            )
            self.intro_image.setText(str(project.settings.intro_image or ""))
            self.restore_presentation(project.settings)
            self.output_name.setText(project.settings.output_name)
            self.music_seed = project.settings.music_seed
            self.open_variant(self.variants.currentIndex())
            self.status.setText(tr("Настройки варианта восстановлены. MP4 и история сохранены."))
            self.find_missing_sources()
        except (OSError, ValueError) as exc:
            self.show_error(str(exc))

    def toggle_output_editor(self) -> None:
        if self.output_editor.isVisible():
            self.hide_output_editor()
            return
        root = Path(self.folder.text().strip())
        videos = list((root / "renders").glob("*.mp4"))
        if not videos:
            return
        latest = getattr(self, "last_result_video", None)
        video = latest if latest in videos else max(videos, key=lambda p: p.stat().st_mtime_ns)
        if Path(self.player.source().toLocalFile()) != video.resolve():
            self.open_video(video, tr("Редактирование готового ролика"))
        self.player.pause()
        self.output_editor.set_duration(self.player.duration())
        self.output_editor.show()
        self.set_output_editor_button(True)
        try:
            snapshot = read_variant(video)
            if snapshot.get("operation") == "cut" and snapshot.get("parent"):
                parent = video.parent / Path(snapshot["parent"]).name
                if parent.is_file():
                    from sculptcut.media import probe

                    duration = probe(parent).duration_us
                    self.open_video(parent, tr("Правки сохранённого варианта"))
                    self.player.pause()
                    if duration:
                        self.output_editor.set_duration(duration // 1000)
                        self.output_editor.cuts = [tuple(c) for c in snapshot["cuts_us"]]
                        self.output_editor.record_cuts([], list(self.output_editor.cuts))
                        self.output_editor.show()
                        self.output_editor.apply_change()
        except (OSError, ValueError, RuntimeError) as exc:
            self.show_error(str(exc))
        self.output_editor.show()
        self.set_output_editor_button(True)
        self.refresh_cut_preview()

    def hide_output_editor(self) -> None:
        """Collapse trimming without losing pending cuts or resizing the main window."""
        self.output_editor.hide()
        self.set_output_editor_button(False)
        self.update_seek_range(self.player.duration())
        self.update_position(self.player.position())

    def show_episodes(self) -> None:
        if self.job is not None:
            return
        root = Path(self.folder.text().strip()).resolve()
        video = self.current_edit_video(root)
        cuts = list(self.output_editor.cuts) if video is not None else []
        try:
            panel = EpisodePanel(root, self, volume_state=self.volume_state, video=video, cuts=cuts,
                                 view_settings=self.recent_settings)
        except (OSError, ValueError) as exc:
            self.show_error(str(exc))
            return
        self.player.pause()
        self.episode_panel = panel
        panel.editor.edit_saved.connect(
            lambda before, after, label: self.history_controller.record_episode(
                root, before, after, label,
            )
        )
        panel.bind_history(self.history_controller.undo_action, self.history_controller.redo_action)
        panel.export_requested.connect(panel.accept)
        result = panel.exec()
        self.episode_panel = None
        panel.deleteLater()
        if result == QDialog.DialogCode.Accepted:
            self.render_episodes(root, video, cuts)

    def current_edit_video(self, root: Path) -> Path | None:
        path = Path(self.player.source().toLocalFile())
        if path.is_file() and path.parent.resolve() in (
            (root / "renders").resolve(), (root / "previews").resolve(),
        ):
            return path.resolve()
        return None

    def render_episodes(
        self, root: Path, video: Path | None = None, cuts: list[tuple[int, int]] | None = None,
    ) -> None:
        if self.job is not None:
            return
        try:
            from sculptcut.combined_edits import prepare_combined_edit, render_combined_edit

            video = video or self.current_edit_video(root)
            if cuts is None:
                cuts = list(self.output_editor.cuts) if video is not None else []
            edit = prepare_combined_edit(root, video, cuts) if video is not None else None
            project, plan = (edit.project, edit.plan) if edit is not None else episode_export(root)
            destination = next_output_path(
                root / "renders",
                (project.settings.output_name or tr("монтаж")) + tr("-исправленный"),
                project.settings.preset,
            )
        except (OSError, ValueError) as exc:
            self.show_error(str(exc))
            return

        def operation(cancel: Event, progress: Callable[[float], None],
                      stage: Callable[[str], None]) -> str:
            stage("Rendering MP4 from originals")
            if edit is not None:
                return str(render_combined_edit(
                    root, edit, destination, cancel, progress,
                    encoder_status=self.encoder_ready.emit,
                ))
            return str(render(root, project, plan, destination, cancel, progress=progress,
                              encoder_status=self.encoder_ready.emit))

        self.job = Job(operation)
        self.job.result.connect(self.show_result)
        self.job.failure.connect(self.show_error)
        self.job.progress.connect(self.show_progress)
        self.job.stage.connect(self.show_stage)
        self.job.finished.connect(self.job_finished)
        self.begin_job(tr("Создание исправленной копии…"))

    def save_output_cuts(self) -> None:
        if self.job or not self.output_editor.cuts:
            return
        video = Path(self.player.source().toLocalFile())
        cuts = list(self.output_editor.cuts)
        root = Path(self.folder.text().strip()).resolve()
        try:
            snapshot = read_variant(video)
            snapshot_project = (
                Project.model_validate(snapshot["project"]) if snapshot.get("project") else None
            )
        except (OSError, ValueError) as exc:
            self.show_error(str(exc))
            return
        if snapshot_project is None or not snapshot.get("plan"):
            unavailable = tr("У этой версии нет монтажного плана. Доступна обычная подрезка.")
        else:
            from sculptcut.combined_edits import prepare_refill_edit

            try:
                prepare_refill_edit(root, video, cuts)
                unavailable = ""
            except (OSError, ValueError) as exc:
                unavailable = error_text(str(exc))
        dialog = CutSaveDialog(unavailable, self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        refill_short = dialog.keep_duration
        dialog.deleteLater()
        if not accepted:
            return
        self.player.pause()
        self.export_note = (
            tr(" Вырезки применены; длительность сохранена. Предыдущая версия сохранена.")
            if refill_short
            else tr(" Вырезки применены; предыдущая версия сохранена.")
        )

        def operation(
            cancel: Event, progress: Callable[[float], None], stage: Callable[[str], None]
        ) -> str:
            if refill_short:
                from sculptcut.shorts_refill import rebuild_after_cuts

                stage(tr("Применяем вырезки и сохраняем длительность"))
                return str(rebuild_after_cuts(root, video, cuts, cancel, progress))
            from sculptcut.combined_edits import (
                pending_episode_edits,
                prepare_combined_edit,
                render_combined_edit,
            )
            from sculptcut.output_cuts import cut_output

            if pending_episode_edits(root, video):
                index = 1
                destination = video.with_name(f"{video.stem}-edit-{index:03d}.mp4")
                while destination.exists():
                    index += 1
                    destination = video.with_name(f"{video.stem}-edit-{index:03d}.mp4")
                return str(render_combined_edit(
                    root, prepare_combined_edit(root, video, cuts), destination, cancel, progress,
                    encoder_status=self.encoder_ready.emit,
                ))
            return str(cut_output(video, cuts, cancel, progress))

        self.job = Job(operation)
        self.job.result.connect(self.show_result)
        self.job.failure.connect(self.show_error)
        self.job.progress.connect(self.show_progress)
        self.job.stage.connect(self.show_stage)
        self.job.finished.connect(self.job_finished)
        self.begin_job(
            tr("Применяем вырезки и сохраняем длительность…")
            if refill_short
            else tr("Сохраняем вырезки в отдельный MP4…")
        )

    def shuffle_music(self) -> None:
        self.music_seed = secrets.randbits(32)
        self.status.setText(tr("Новый подбор музыки. Соберите предпросмотр, чтобы послушать."))

    def add_videos(self) -> None:
        if self.job:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            tr("Выберите одно или несколько видео"),
            "",
            tr("Видео (*.mp4 *.mkv *.mov *.webm)"),
        )
        if paths:
            self.add_dropped_videos([Path(p) for p in paths])

    def play_pause(self) -> None:
        if self.player.source().isEmpty():
            return
        self.pause_on_frame = False
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            if self.output_editor.isVisible() and self.output_editor.ranges:
                ranges = self.output_editor.ranges
                if self.player.position() * 1000 >= ranges[-1][1]:
                    self.player.setPosition(ranges[0][0] // 1000)
            self.player.play()

    def playback_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.play_button.setText(tr("Пауза") if playing else tr("Просмотр"))
        self.play_button.setIcon(ui_icon("pause" if playing else "play"))
        self.play_button.setToolTip(
            tr("Поставить воспроизведение на паузу") if playing else tr("Воспроизвести видео")
        )

    def set_output_editor_button(self, opened: bool) -> None:
        self.cut_button.setChecked(opened)
        self.cut_button.setIcon(ui_icon(
            "chevron_down" if opened else "chevron_up", "#8ed5ca",
        ))
        self.versions_panel.setVisible(self.variants.count() > 0)
        self.versions_panel.setEnabled(not opened and self.job is None)
        self.update_preview_minimum()
        if opened:
            self.cut_button.setToolTip(tr("Скрыть инструменты подрезки ролика"))
        else:
            self.cut_button.setToolTip(tr("Открыть инструменты выбора и удаления участков видео"))

    def confirm_action(self, title: str, message: str, confirm_text: str, reject_text: str) -> bool:
        """Ask before an operation that can stop work or close the application."""
        dialog = QMessageBox(QMessageBox.Icon.Warning, title, message, parent=self)
        dialog.setInformativeText(
            tr("Готовые файлы сохранятся; временные файлы будут убраны безопасно.")
        )
        reject = dialog.addButton(reject_text, QMessageBox.ButtonRole.RejectRole)
        confirm = dialog.addButton(confirm_text, QMessageBox.ButtonRole.DestructiveRole)
        confirm.setObjectName("destructiveButton")
        dialog.setDefaultButton(reject)
        dialog.setEscapeButton(reject)
        style_message_box(dialog)
        dialog.exec()
        confirmed = dialog.clickedButton() is confirm
        dialog.deleteLater()
        return confirmed

    def request_cancellation(self) -> None:
        if not self.job or self.job.cancel.is_set():
            return
        self.job.cancel.set()
        self.cancel_button.setEnabled(False)
        self.status.setText(tr("Отмена… Дожидаемся безопасной остановки обработки."))

    def cancel_job(self) -> None:
        if not self.job or self.job.cancel.is_set():
            return
        if self.confirm_action(
            tr("Отменить обработку?"),
            tr("Текущая операция будет остановлена после безопасного завершения шага."),
            tr("Отменить обработку"),
            tr("Продолжить обработку"),
        ):
            self.request_cancellation()

    def finish_close(self, event: QCloseEvent) -> None:
        self.updates.shutdown()
        self.autosave_draft()
        self.draft_timer.stop()
        self.seek.shutdown()
        if self.episode_panel is not None:
            self.episode_panel.shutdown()
        self.player.stop()
        self.recent_settings.setValue("session_running", False)
        self.recent_settings.sync()
        event.accept()

    def has_unsaved_close_state(self) -> bool:
        return bool(
            self.folder.text().strip()
            or self.sources.count()
            or self.output_name.text().strip()
            or self.music.text().strip()
            or self.intro_image.text().strip()
            or self.presentation_video is not None
            or not self.player.source().isEmpty()
            or self.target.value() != 10
            or self.preset.currentData() != "timelapse"
            or self.output_editor.cuts
        )

    def handle_close_request(self, event: QCloseEvent) -> None:
        if self.job:
            confirmed = self.confirm_action(
                tr("Закрыть ArtLapse?"),
                tr("Обработка будет отменена, а окно закроется после безопасной остановки."),
                tr("Отменить и закрыть"),
                tr("Продолжить обработку"),
            )
            if not confirmed:
                event.ignore()
                return
            self.close_after_job = True
            self.request_cancellation()
            if self.job:
                event.ignore()
            else:
                self.finish_close(event)
            return
        if not self.has_unsaved_close_state():
            self.finish_close(event)
            return
        if self.confirm_action(
            tr("Закрыть ArtLapse?"),
            tr("Закрыть приложение? Настройки формы сохранятся для восстановления; "
            "проект сохраняется кнопкой «Сохранить»."),
            tr("Закрыть приложение"),
            tr("Остаться в приложении"),
        ):
            self.finish_close(event)
        else:
            event.ignore()

    def show_error(self, message: str) -> None:
        self.last_error = message
        self.status.setText(error_text(message))
        self.status.setToolTip(message)
        self.error_details_button.show()

    def show_error_details(self) -> None:
        if not self.last_error:
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle(tr("Подробности ошибки"))
        dialog.setIcon(QMessageBox.Icon.Warning)
        message = self.last_error
        dialog.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        dialog.setText(error_text(message).removesuffix(tr(' Нажмите «Подробности ошибки».')))
        dialog.setDetailedText(self.last_error)
        dialog.setStandardButtons(QMessageBox.StandardButton.Ok)
        copy_button = dialog.addButton(tr("Копировать ошибку"), QMessageBox.ButtonRole.ActionRole)
        copy_button.clicked.connect(
            lambda: QApplication.clipboard().setText(message)
        )
        style_message_box(dialog)
        dialog.exec()
        dialog.deleteLater()

    def start_action(self, action: str) -> None:
        if self.job:
            return
        if self.find_missing_sources(lambda: self.start_action(action)):
            return
        if not self.folder.text().strip() or not self.source_paths():
            self.show_error("Choose a project folder and add at least one video.")
            return
        try:
            name = clean_output_name(self.output_name.text())
            if action == "create" and not name:
                self.show_error(tr("Введите имя файла перед созданием ролика."))
                self.output_name.setFocus()
                return
            target = round(self.target.value() if self.duration_in_seconds
                           else self.target.value() * 60)
            if target <= 0:
                raise ValueError(tr("Укажите длительность больше нуля."))
            self.output_name.setText(name)
            root = Path(self.folder.text()).resolve()
            paths = self.source_paths()
            preset = self.preset.currentData()
            music = Path(self.music.text()).resolve() if self.music.text().strip() else None
            if preset in MUSIC_SHORTS_PRESETS and music is None:
                raise ValueError(tr("Для шортса под музыку выберите файл или папку с музыкой."))
            intro = (Path(self.intro_image.text()).resolve()
                     if self.intro_image.text().strip() else None)
            if (self.presentation_video is None and intro
                    and (not intro.is_file() or QPixmap(str(intro)).isNull())):
                raise ValueError(
                    tr("Выберите существующее изображение заставки или очистите поле.")
                )
            base = (load_project(root).settings if (root / "project.json").is_file()
                    else ProjectSettings())
            settings = ProjectSettings.model_validate(base.model_dump() | {
                "target_seconds": target, "preset": preset,
                "music_path": music if music and music.is_file() else None,
                "music_folder": music if music and not music.is_file() else None,
                "intro_image": intro,
                **self.presentation_values(),
            })
        except (OSError, ValueError) as exc:
            self.show_error(str(exc))
            return
        snapshot = self.form_draft()
        passed = False

        def operation(cancel: Event, progress: Callable[[float], None],
                      stage: Callable[[str], None]) -> str:
            report = check_creation(root, paths, settings, cancel, stage)
            return f"{report.source_count}:{report.music_count}:{report.required_bytes}"

        def checked(result: str) -> None:
            nonlocal passed
            passed = True
            self.job_succeeded = True
            self.status.setText(tr("Проверка пройдена. Подготовка к созданию ролика…"))

        def continue_creation() -> None:
            if passed and not checking.cancel.is_set() and not self._allow_close:
                if snapshot != self.form_draft():
                    self.show_error(
                        tr("Настройки изменились во время проверки. Нажмите «Создать ролик» снова.")
                    )
                    return
                self._start_checked_action(action)

        checking = Job(operation)
        checking.setObjectName("preflight")
        self.job = checking
        checking.result.connect(checked)
        checking.failure.connect(self.show_error)
        checking.stage.connect(self.show_stage)
        checking.finished.connect(self.job_finished)
        checking.finished.connect(continue_creation)
        self.begin_job(tr("Быстрая проверка перед созданием…"))

    def _start_checked_action(self, action: str) -> None:
        if self.job:
            return
        if self.find_missing_sources(lambda: self.start_action(action)):
            return
        if not self.folder.text().strip() or not self.source_paths():
            self.show_error("Choose a project folder and add at least one video.")
            return
        try:
            output_name = clean_output_name(self.output_name.text())
        except ValueError as exc:
            self.show_error(str(exc))
            self.output_name.setFocus()
            return
        if action == "create" and not output_name:
            self.show_error(tr("Введите имя файла перед созданием ролика."))
            self.output_name.setFocus()
            return
        self.output_name.setText(output_name)
        root = Path(self.folder.text()).resolve()
        paths = self.source_paths()
        target = round(
            self.target.value() if self.duration_in_seconds else self.target.value() * 60
        )
        if target <= 0:
            self.show_error(tr("Укажите длительность больше нуля."))
            return
        preset = self.preset.currentData()
        frame_crop_mode = str(self.frame_mode.currentData()) if preset == "timelapse" else "full"
        shorts_layout = str(self.shorts_layout.currentData())
        if preset in SHORTS_PRESETS and shorts_layout == "square":
            for path in paths:
                if not self.choose_square_area(path):
                    return
        if self.webcam_enabled.isChecked() and not self.choose_webcam_areas():
            return
        webcam_enabled = self.webcam_enabled.isChecked()
        webcam_rois = self.webcam_rois.copy()
        square_rois = self.square_rois.copy()
        if frame_crop_mode == "viewport" and not self.crop_customized:
            self.choose_crop_area()
            if not self.crop_customized:
                return
        frame_crop_roi = self.crop_roi
        sculpt_only = preset in ("shorts", "beat_shorts", "music_shorts")
        music_input = Path(self.music.text()).resolve() if self.music.text().strip() else None
        music_path = music_input if music_input is not None and music_input.is_file() else None
        music_folder = music_input if music_path is None else None
        if preset in MUSIC_SHORTS_PRESETS and music_input is None:
            self.show_error(tr("Для шортса под музыку выберите файл или папку с музыкой."))
            return
        if music_folder is not None and not music_folder.is_dir():
            self.show_error(tr("Выберите существующую папку с музыкой или очистите поле."))
            return
        intro_image = (
            Path(self.intro_image.text()).resolve() if self.intro_image.text().strip() else None
        )
        if self.presentation_video is None and intro_image is not None and (
            not intro_image.is_file() or QPixmap(str(intro_image)).isNull()
        ):
            self.show_error(tr("Выберите существующее изображение заставки или очистите поле."))
            return
        presentation_values = self.presentation_values()
        music_seed = self.music_seed
        music_selection_mode = cast(
            Literal["dynamic", "full_tracks"], self.music_selection.currentData(),
        )
        beat_sync_enabled = preset in MUSIC_SHORTS_PRESETS
        self.root = root
        self.last_error = ""
        self.player.pause()
        self.export_note = ""
        self.statistics.clear()

        # Capture the selected version before the worker changes project settings
        # or result playback resets the output editor.
        edit_video = self.current_edit_video(root)
        pending_cuts = list(self.output_editor.cuts) if edit_video else []

        def operation(
            cancel: Event, progress: Callable[[float], None], stage: Callable[[str], None]
        ) -> str:
            def phase(message: str) -> None:
                logging.getLogger("artlapse").info(message, extra={"event": "job_stage"})
                stage(message)
                progress(-1)

            phase("Reading video metadata and preparing project")
            project = (
                load_project(root)
                if (root / "project.json").exists()
                else create_project(root, paths)
            )
            from sculptcut.combined_edits import (
                creation_edit,
                prepare_combined_edit,
                render_combined_edit,
            )
            from sculptcut.variants import has_manual_edits

            previous_edit = (
                prepare_combined_edit(root, edit_video, pending_cuts, apply_current=False)
                if edit_video and (pending_cuts or has_manual_edits(edit_video)) else None
            )
            project = replace_sources(project, paths)
            position = sum(s.duration_us for s in project.sources)
            settings = ProjectSettings.model_validate(
                project.settings.model_dump()
                | {
                    "webcam_enabled": webcam_enabled,
                    "webcam_rois": {
                        source.id: webcam_rois[source.path.resolve()]
                        for source in project.sources if source.path.resolve() in webcam_rois
                    },
                    "target_seconds": target,
                    "preset": preset,
                    "crop_mode": frame_crop_mode,
                    "timelapse_viewport_roi": frame_crop_roi,
                    "shorts_layout": shorts_layout,
                    "shorts_square_rois": {
                        source.id: square_rois[source.path.resolve()]
                        for source in project.sources if source.path.resolve() in square_rois
                    },
                    "sculpt_only": sculpt_only,
                    "music_path": music_path,
                    "music_folder": music_folder,
                    "music_seed": music_seed,
                    "music_selection_mode": music_selection_mode,
                    "beat_sync_enabled": beat_sync_enabled,
                    "beat_sync_mode": DEFAULT_BEAT_SYNC_MODE,
                    "beat_sync_strength": DEFAULT_BEAT_SYNC_STRENGTH,
                    "intro_image": intro_image,
                    **presentation_values,
                    "output_name": output_name,
                }
            )
            # Episode edits belong to source time, shared by every automatic preset.
            project = Project.model_validate(
                project.model_dump() | {"settings": settings}
            )
            save_project(root, project)
            needs_analysis = preset not in (
                "speed", "photoshop_shorts", "photoshop_beat_shorts", "photoshop_music_shorts",
            )
            if needs_analysis:
                connection = database(root)
                try:
                    needs_analysis = False
                    for source in project.sources:
                        checkpoint = connection.execute(
                            "SELECT position FROM checkpoints "
                            "WHERE stage='features' AND source_id=? AND key=?",
                            (source.id, analysis_key(project, source)),
                        ).fetchone()
                        if (
                            not checkpoint
                            or not proxy_path(root, project, source).is_file()
                            or checkpoint[0]
                            < source.duration_us - 2_000_000 / project.settings.proxy_fps
                        ):
                            needs_analysis = True
                            break
                finally:
                    connection.close()
            if needs_analysis:
                previous_stage = ""

                def proxy_progress(message: str, value: float) -> None:
                    nonlocal previous_stage
                    if message != previous_stage:
                        phase(message)
                        previous_stage = message
                    progress(value)

                make_proxies(root, project, cancel, proxy_progress)
                phase("Measuring frame changes")
                extract_features(
                    root, project, cancel, lambda t: progress(min(1, t / max(1, position)))
                )
            phase("Updating the edit plan")
            planning_project = (project.model_copy(update={"overrides": []})
                                if previous_edit else project)
            plan = plan_project(root, planning_project, target, cancel, progress, phase)
            combined = creation_edit(project, plan, previous_edit) if previous_edit else None
            if combined:
                plan = combined.plan
            self.statistics_ready.emit(statistics_text(plan))
            if plan.settings.get("beat_still_fallback") == "motion":
                self.export_note = (
                    tr(" Не хватило разных кадров для ритма; музыка сохранена, "
                    "вместо стоп-кадров использованы видеоэпизоды.")
                )
            elif sculpt_only and plan.actual_frame_count < plan.target_frame_count:
                minutes = plan.actual_frame_count / plan.output_fps / 60
                self.export_note = (
                    tr(f" Процесс: {minutes:.2f} мин — без добавления пауз ради длительности.")
                )
            if cancel.is_set():
                raise Cancelled("Cancelled")
            preview = action == "preview"
            folder = root / ("previews" if preview else "renders")
            destination = next_output_path(
                folder,
                "" if preview else output_name,
                "" if preview else preset,
            )
            phase("Rendering preview" if preview else "Rendering MP4 from originals")
            if combined:
                return str(render_combined_edit(
                    root, combined, destination, cancel, progress,
                    preview=preview, encoder_status=self.encoder_ready.emit,
                ))
            return str(
                render(
                    root,
                    project,
                    plan,
                    destination,
                    cancel,
                    preview,
                    progress=progress,
                    encoder_status=self.encoder_ready.emit,
                )
            )

        self.job = Job(operation)
        self.job.result.connect(self.show_result)
        self.job.failure.connect(self.show_error)
        self.job.progress.connect(self.show_progress)
        self.job.stage.connect(self.show_stage)
        self.job.finished.connect(self.job_finished)
        self.begin_job(tr("Подготовка к обработке…"))

    def begin_job(self, message: str, *, detailed_feedback: bool = True) -> None:
        if self.job is None:
            return
        self.job.stage.connect(self.job_stage.emit)
        self.detailed_job_feedback = detailed_feedback
        self.job_started_at = monotonic()
        self.stage_started_at = self.job_started_at
        self.current_stage = message
        self.stage_progress = None
        self.encoder_name = ""
        self.job_succeeded = False
        self.last_error = ""
        for widget in self.controls:
            widget.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.show_progress(-1)
        self.progress_details.setVisible(detailed_feedback)
        self.progress_timer.start()
        self.update_progress_details()
        self.status.setToolTip("")
        self.error_details_button.hide()
        self.status.setText(message)
        self.history_controller.refresh()
        self.job.start()

    def show_progress(self, value: float) -> None:
        visible = self.job is not None and getattr(self, "detailed_job_feedback", True)
        self.progress_bar.setVisible(visible)
        self.cancel_button.setVisible(visible)
        self.update_playback_controls()
        if value < 0:
            self.progress_bar.setRange(0, 0)
            self.stage_progress = None
        else:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(round(value * 100))
            self.stage_progress = max(0.0, min(1.0, value))
        self.update_progress_details()

    def show_stage(self, message: str) -> None:
        if self.job and not self.job.cancel.is_set():
            self.current_stage = stage_text(message)
            self.stage_started_at = monotonic()
            self.stage_progress = None
            self.status.setText(self.current_stage)
            self.update_progress_details()

    @staticmethod
    def format_runtime(seconds: float) -> str:
        return format_time(seconds)

    def show_statistics(self, text: str) -> None:
        self.statistics.setText(text)
        self.statistics.setVisible(bool(text))

    def set_encoder_name(self, name: str) -> None:
        self.encoder_name = tr(name)
        self.update_progress_details()

    def update_progress_details(self) -> None:
        if self.job_started_at is None:
            return
        now = monotonic()
        parts = [self.current_stage, tr(f"прошло {self.format_runtime(now - self.job_started_at)}")]
        if (
            self.stage_started_at is not None
            and self.stage_progress is not None
            and 0.02 <= self.stage_progress < 1
        ):
            stage_elapsed = now - self.stage_started_at
            remaining = stage_elapsed * (1 - self.stage_progress) / self.stage_progress
            parts.append(tr(f"осталось примерно {self.format_runtime(remaining)}"))
        elif self.stage_progress != 1:
            parts.append(tr("остаток рассчитывается"))
        if self.encoder_name:
            parts.append(tr("кодирование: ") + self.encoder_name)
        self.progress_details.setText(" · ".join(parts))
        if not self.last_error:
            self.status.setToolTip(self.progress_details.text())

    def show_result(self, result: str) -> None:
        self.job_succeeded = True
        self.invalidate_episodes()
        self.status.setText(tr("Сохранено: ") + result + getattr(self, "export_note", ""))
        self.show_progress(1)
        path = Path(result)
        if path.suffix == ".mp4":
            self.last_result_video = path.resolve()
            if path.parent.name in ("renders", "previews"):
                self.remember_project(path.parent.parent)
            if path.parent.name == "renders" and self.music.text().strip():
                self.music_seed = secrets.randbits(32)
                try:
                    project = load_project(path.parent.parent)
                    settings = project.settings.model_copy(update={"music_seed": self.music_seed})
                    save_project(
                        path.parent.parent, project.model_copy(update={"settings": settings}),
                    )
                except (OSError, ValueError) as exc:
                    logging.getLogger("artlapse").warning(
                        "Video saved, but next music selection could not be persisted: %s", exc,
                    )
                    self.status.setText(
                        self.status.text()
                        + tr(" Настройки следующего подбора музыки не сохранены: ")
                        + error_text(str(exc))
                    )
            shorts = self.preset.currentData() in SHORTS_PRESETS
            title = (
                tr("Предпросмотр шортса")
                if shorts and path.parent.name == "previews"
                else tr("Готовый шортс")
                if shorts
                else tr("Готовый таймлапс")
            )
            self.open_video(path, title, autoplay=True)
            self.update_result_actions()
            self.refresh_variants()
        elif path.suffix == ".png":
            self.intro_preview.setPixmap(
                QPixmap(str(path)).scaled(
                    52,
                    34,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            self.intro_preview.show()

    def job_finished(self) -> None:
        self.progress_timer.stop()
        self.progress_details.hide()
        if self.job_started_at is not None:
            elapsed = monotonic() - self.job_started_at
            prefix = tr("Завершено за ") if self.job_succeeded else tr("Остановлено через ")
            self.progress_details.setText(prefix + self.format_runtime(elapsed))
        self.job_started_at = None
        if self.progress_bar.maximum() == 0:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(0)
        if self.job:
            self.job.deleteLater()
        self.job = None
        for widget in self.controls:
            widget.setEnabled(True)
        self.update_playback_controls()
        self.update_result_actions()
        self.update_variant_tooltip()
        self.versions_panel.setEnabled(not self.output_editor.isVisible())
        self.cancel_button.setEnabled(False)
        self.cancel_button.hide()
        self.progress_bar.hide()

        self.target.setEnabled(True)
        self.history_controller.refresh()
        if self.close_after_job:
            self.close_after_job = False
            self._allow_close = True
            self.close()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if (event.key() == Qt.Key.Key_Escape and self.output_editor.isVisible()
                and self.job is None):
            self.hide_output_editor()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._allow_close or not event.spontaneous():
            self.finish_close(event)
            return
        self.handle_close_request(event)


def main() -> int:
    configure_logging()
    from sculptcut.application_identity import configure_application_identity
    from sculptcut.performance import configure_analysis_threads

    configure_analysis_threads()
    configure_application_identity()
    application = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    QTimer.singleShot(0, window.recover_session)
    window.updates.schedule_startup()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
