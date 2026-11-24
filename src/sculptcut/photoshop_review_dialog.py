"""Read-only comparison of saved montage frames and their original timestamps."""

from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPaintEvent, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from sculptcut.episode_images import EpisodeImages, ImageKey, image_key
from sculptcut.i18n import tr
from sculptcut.photoshop_review import AlignmentMarker
from sculptcut.time_format import format_time


class AlignmentTimeline(QWidget):
    selected = Signal(int)

    def __init__(self, duration: int, markers: list[AlignmentMarker]) -> None:
        super().__init__()
        self.duration, self.markers = max(1, duration), markers
        self.active = -1
        self.setFixedHeight(22)
        self.setToolTip(tr('Нажмите метку, чтобы сравнить кадры'))

    def x_at(self, frame: int) -> int:
        return 5 + round(frame / self.duration * max(1, self.width()-10))

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(5, 9, self.width()-10, 4, QColor('#485669'))
        for i, marker in enumerate(self.markers):
            left, right = self.x_at(marker.start), self.x_at(marker.end)
            color = '#ffe0a0' if i == self.active else '#cb9651'
            painter.fillRect(left, 5, max(3, right-left), 12, QColor(color))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        x = event.position().x()
        hits = [(abs(x-self.x_at(m.start)), i) for i, m in enumerate(self.markers)
                if self.x_at(m.start)-4 <= x <= max(self.x_at(m.end), self.x_at(m.start)+3)+4]
        if hits:
            self.selected.emit(min(hits)[1])


class ComparisonImage(QLabel):
    def __init__(self, title: str, owner: QWidget) -> None:
        super().__init__()
        self.setAccessibleName(title)
        self.setMinimumSize(160, 180)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet('background:#0c1118; border-radius:6px;')
        self.images = EpisodeImages(owner, width=800, limit=8)
        self.images.ready.connect(self.show_frame)
        self.images.failed.connect(lambda _: self.setText(tr('Кадр недоступен')))
        self.frame = QPixmap()

    def request(self, path: Path | None, time_us: int) -> None:
        self.frame = QPixmap()
        try:
            key = image_key(path, time_us) if path is not None else None
        except OSError:
            key = None
        self.images.request([key] if key else [])
        self.setText(tr('Загружаем кадр…') if key else tr('Файл недоступен'))

    def show_frame(self, key: ImageKey, image: QImage) -> None:
        self.frame = QPixmap.fromImage(image)
        self.fit_frame()

    def fit_frame(self) -> None:
        if not self.frame.isNull():
            self.setPixmap(self.frame.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                             Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.fit_frame()


class PhotoshopReviewDialog(QDialog):
    edit_requested = Signal(str, object)

    def __init__(
        self, video: Path, sources: dict[str, Path], fps: int, duration: int,
        markers: list[AlignmentMarker], parent: QWidget, *, editable: bool = False,
    ) -> None:
        super().__init__(parent)
        self.video, self.sources, self.fps, self.markers = video, sources, fps, markers
        self.setWindowTitle(tr('Проверить перед экспортом'))
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        layout = QVBoxLayout(self)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText(tr('Закрыть'))
        buttons.rejected.connect(self.reject)
        self.timer = QTimer(self, singleShot=True, interval=60)
        if not markers:
            info = QLabel(tr('В сохранённом плане нет меток потери привязки.'))
            info.setWordWrap(True)
            layout.addWidget(info)
            layout.addWidget(buttons)
            return

        self.resize(910, 580)
        self.timeline = AlignmentTimeline(duration, markers)
        layout.addWidget(self.timeline)
        self.timeline.selected.connect(self.select_marker)
        self.choices = QComboBox()
        self.choices.setAccessibleName(tr('Метки потери привязки'))
        self.choices.addItems([tr('Потеря привязки · {0}–{1}').format(
            format_time(m.start/fps, 2), format_time(m.end/fps, 2)) for m in markers])
        self.choices.currentIndexChanged.connect(self.marker_changed)
        layout.addWidget(self.choices)

        images = QHBoxLayout()
        self.montage = ComparisonImage(tr('Монтаж'), self)
        self.original = ComparisonImage(tr('Исходник'), self)
        self.montage_title = QLabel()
        self.original_title = QLabel()
        self.original_title.setWordWrap(True)
        for title, image in ((self.montage_title, self.montage),
                             (self.original_title, self.original)):
            column = QVBoxLayout()
            column.addWidget(title)
            column.addWidget(image, 1)
            images.addLayout(column, 1)
        layout.addLayout(images, 1)
        self.frame_slider = QSlider(Qt.Orientation.Horizontal)
        self.frame_slider.setAccessibleName(tr('Кадр внутри метки'))
        self.frame_slider.valueChanged.connect(lambda _: self.timer.start())
        self.timer.timeout.connect(self.request_frames)
        layout.addWidget(self.frame_slider)
        if editable:
            edit = buttons.addButton(tr('Открыть эпизод в редакторе'),
                                     QDialogButtonBox.ButtonRole.ActionRole)
            edit.setToolTip(tr('Перейти к этому моменту для ручной подрезки'))
            edit.clicked.connect(self.open_editor)
        layout.addWidget(buttons)
        self.marker_changed(0)

    def select_marker(self, index: int) -> None:
        if index == self.choices.currentIndex():
            self.marker_changed(index)
        else:
            self.choices.setCurrentIndex(index)

    def marker_changed(self, index: int) -> None:
        marker = self.markers[index]
        self.timeline.active = index
        self.timeline.update()
        with QSignalBlocker(self.frame_slider):
            self.frame_slider.setRange(marker.start, marker.end-1)
            self.frame_slider.setValue(marker.start)
        self.frame_slider.setEnabled(marker.end-marker.start > 1)
        self.timer.stop()
        self.request_frames()

    def request_frames(self) -> None:
        marker = self.markers[self.choices.currentIndex()]
        frame = self.frame_slider.value()
        original_time = marker.original_time(frame)
        path = self.sources.get(marker.decision.source_id)
        self.montage_title.setText(tr('Монтаж')+' · '+format_time(frame/self.fps, 2))
        self.original_title.setText(tr('Исходник')+' · '+format_time(original_time/1e6, 2)
                                    + (' · '+path.name if path is not None else ''))
        self.montage.request(self.video, frame*1_000_000//self.fps)
        self.original.request(path, original_time)

    def open_editor(self) -> None:
        marker = self.markers[self.choices.currentIndex()]
        self.edit_requested.emit(marker.decision.source_id,
                                 marker.original_time(self.frame_slider.value()))
        self.accept()

    def done(self, result: int) -> None:
        self.timer.stop()
        if self.markers:
            self.montage.images.shutdown()
            self.original.images.shutdown()
        super().done(result)
