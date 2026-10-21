"""One fixed crop rectangle for the complete ordinary timelapse."""

from pathlib import Path

import cv2
from PySide6.QtCore import QPointF, QRectF, Qt, QThread, Signal
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from sculptcut.cropping import fit_video_aspect
from sculptcut.domain import Roi
from sculptcut.i18n import tr
from sculptcut.ui_theme import DialogButtons, apply_dialog_theme, dialog_header


def preview_frame(path: Path, fraction: float = 0.1) -> QImage:
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise ValueError("Cannot open source video for crop selection")
        count = capture.get(cv2.CAP_PROP_FRAME_COUNT)
        if count > 10:
            capture.set(cv2.CAP_PROP_POS_FRAMES, min(int(count * fraction), int(count) - 1))
        ok, frame = capture.read()
        if not ok:
            capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = capture.read()
        if not ok:
            raise ValueError("Cannot read source frame for crop selection")
    finally:
        capture.release()
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    height, width = rgb.shape[:2]
    image = QImage(rgb.data, width, height, 3 * width, QImage.Format.Format_RGB888).copy()
    return image.scaled(
        800, 480, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
    )


class PreviewLoader(QThread):
    loaded = Signal(object)
    failed = Signal(str)

    def __init__(self, source: Path, fraction: float | None = None) -> None:
        super().__init__()
        self.source = source
        self.fraction = fraction

    def run(self) -> None:
        try:
            self.loaded.emit(preview_frame(self.source) if self.fraction is None
                             else preview_frame(self.source, self.fraction))
        except Exception as exc:
            self.failed.emit(str(exc))


# A dialog may close while a slow source is still being read. Keep its thread alive.
_active_loaders: set[PreviewLoader] = set()


class CropCanvas(QWidget):
    def __init__(self, frame: QPixmap, roi: Roi, aspect: float = 16 / 9) -> None:
        super().__init__()
        self.frame = frame
        self.aspect = aspect
        self.roi = fit_video_aspect(roi, frame.width(), frame.height(), aspect) if aspect else roi
        self.anchor: QPointF | None = None
        self.move_origin: Roi | None = None
        self.setFixedSize(frame.size())
        self.setCursor(Qt.CursorShape.CrossCursor)

    def paintEvent(self, _event: object) -> None:
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self.frame)
        area = QRectF(
            self.roi.x * self.width(),
            self.roi.y * self.height(),
            self.roi.width * self.width(),
            self.roi.height * self.height(),
        )
        if not self.aspect:
            painter.fillRect(area, QColor(255, 80, 50, 100))
        shade = QColor(0, 0, 0, 130 if self.aspect else 0)
        painter.fillRect(QRectF(0, 0, self.width(), area.top()), shade)
        painter.fillRect(
            QRectF(0, area.bottom(), self.width(), self.height() - area.bottom()), shade
        )
        painter.fillRect(QRectF(0, area.top(), area.left(), area.height()), shade)
        painter.fillRect(
            QRectF(area.right(), area.top(), self.width() - area.right(), area.height()), shade
        )
        painter.setPen(QPen(QColor("#ff765e"), 3))
        painter.drawRect(area)
        painter.setBrush(QColor("#ff765e"))
        for corner in (area.topLeft(), area.topRight(), area.bottomLeft(), area.bottomRight()):
            painter.drawEllipse(corner, 6, 6)

    def _resize_anchor(self, point: QPointF) -> QPointF | None:
        corners = (
            (self.roi.x, self.roi.y, self.roi.x + self.roi.width, self.roi.y + self.roi.height),
            (self.roi.x + self.roi.width, self.roi.y, self.roi.x, self.roi.y + self.roi.height),
            (self.roi.x, self.roi.y + self.roi.height, self.roi.x + self.roi.width, self.roi.y),
            (self.roi.x + self.roi.width, self.roi.y + self.roi.height, self.roi.x, self.roi.y),
        )
        for x, y, opposite_x, opposite_y in corners:
            if abs(point.x() - x) * self.width() <= 14 and abs(point.y() - y) * self.height() <= 14:
                return QPointF(opposite_x, opposite_y)
        return None

    def _position(self, event: QMouseEvent) -> QPointF:
        return QPointF(
            max(0.0, min(1.0, event.position().x() / self.width())),
            max(0.0, min(1.0, event.position().y() / self.height())),
        )

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            point = self._position(event)
            resize_anchor = self._resize_anchor(point)
            self.anchor = resize_anchor or point
            if resize_anchor is not None:
                self.move_origin = None
                self.setCursor(Qt.CursorShape.SizeFDiagCursor)
                return
            if (
                self.roi.x < point.x() < self.roi.x + self.roi.width
                and self.roi.y < point.y() < self.roi.y + self.roi.height
            ):
                self.move_origin = self.roi
                self.setCursor(Qt.CursorShape.SizeAllCursor)
            else:
                self.move_origin = None

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.anchor is None:
            self.setCursor(
                Qt.CursorShape.SizeFDiagCursor
                if self._resize_anchor(self._position(event)) is not None
                else Qt.CursorShape.SizeAllCursor
                if self.roi.x < event.position().x() / self.width() < self.roi.x + self.roi.width
                and self.roi.y < event.position().y() / self.height() < self.roi.y + self.roi.height
                else Qt.CursorShape.CrossCursor
            )
            return
        point = self._position(event)
        if self.move_origin is not None:
            origin = self.move_origin
            self.roi = Roi(
                x=max(0.0, min(1.0 - origin.width, origin.x + point.x() - self.anchor.x())),
                y=max(0.0, min(1.0 - origin.height, origin.y + point.y() - self.anchor.y())),
                width=origin.width,
                height=origin.height,
            )
            self.update()
            return
        dx = abs(point.x() - self.anchor.x()) * self.width()
        dy = abs(point.y() - self.anchor.y()) * self.height()
        pixel_width = max(dx, dy * self.aspect) if self.aspect else dx
        pixel_height = pixel_width / self.aspect if self.aspect else dy
        rightward = point.x() >= self.anchor.x()
        downward = point.y() >= self.anchor.y()
        available_width = (1 - self.anchor.x() if rightward else self.anchor.x()) * self.width()
        available_height = (1 - self.anchor.y() if downward else self.anchor.y()) * self.height()
        if pixel_width > 0 and pixel_height > 0:
            scale = min(1.0, available_width / pixel_width, available_height / pixel_height)
            width, height = pixel_width * scale / self.width(), pixel_height * scale / self.height()
        else:
            width = height = 0.0
        if width >= (0.1 if self.aspect else 0.02) and height >= (0.1 if self.aspect else 0.02):
            x = self.anchor.x() if rightward else self.anchor.x() - width
            y = self.anchor.y() if downward else self.anchor.y() - height
            self.roi = Roi(x=x, y=y, width=width, height=height)
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.mouseMoveEvent(event)
            self.anchor = None
            self.move_origin = None
            self.setCursor(Qt.CursorShape.CrossCursor)


class TimelapseCropDialog(QDialog):
    def __init__(
        self, source: Path, roi: Roi, parent: QWidget | None = None, *, square: bool = False,
        initial_frame: QImage | None = None,
    ) -> None:
        super().__init__(parent)
        apply_dialog_theme(self)
        self.aspect = 1.0 if square else 16 / 9
        self.setWindowTitle(
            tr("Квадрат Shorts — ") + source.name if square else tr("Рамка таймлапса")
        )
        self.content = QVBoxLayout(self)
        self.content.setContentsMargins(20, 20, 20, 18)
        self.content.setSpacing(16)
        header = dialog_header(tr("Квадрат Shorts") if square else tr("Рамка таймлапса"), "crop")
        header.setToolTip(tr("Тяните за углы, чтобы изменить размер рамки, или внутри неё, "
                          "чтобы переместить. Снаружи можно нарисовать новую рамку."))
        self.content.addWidget(header)
        self.initial_roi = roi
        self.canvas: CropCanvas | None = None
        self.loading = QLabel(tr("Загрузка кадра исходного видео…"))
        self.loading.setObjectName("sectionHint")
        self.content.addWidget(self.loading)
        self.buttons = DialogButtons(self)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.content.addWidget(self.buttons)
        if initial_frame is not None and not initial_frame.isNull():
            self.show_frame(initial_frame)
            return
        self.loader = PreviewLoader(source)
        _active_loaders.add(self.loader)
        self.loader.finished.connect(lambda: _active_loaders.discard(self.loader))
        self.loader.loaded.connect(self.show_frame)
        self.loader.failed.connect(self.show_load_error)
        self.loader.start()

    def show_frame(self, image: QImage) -> None:
        self.loading.hide()
        self.canvas = CropCanvas(QPixmap.fromImage(image), self.initial_roi, self.aspect)
        self.content.insertWidget(2, self.canvas, alignment=Qt.AlignmentFlag.AlignCenter)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)

    def show_load_error(self, message: str) -> None:
        self.loading.setText(tr(f"Не удалось загрузить кадр: {message}"))

    @property
    def roi(self) -> Roi:
        if self.canvas is None:
            raise ValueError("The crop frame is still loading")
        return self.canvas.roi


class WebcamDialog(QDialog):
    """Choose an analysis-only rectangle on a seekable source preview."""

    def __init__(
        self, source: Path, roi: Roi | None, parent: QWidget | None = None, *,
        initial_frame: QImage | None = None,
    ) -> None:
        super().__init__(parent)
        apply_dialog_theme(self)
        self.setWindowTitle(tr("Область веб-камеры — ") + source.name)
        self.source = source
        self.initial_frame = initial_frame
        self.selection = roi or Roi(x=0.02, y=0.7, width=0.25, height=0.28)
        self.canvas: CropCanvas | None = None
        self.generation = 0
        self.content = QVBoxLayout(self)
        self.content.setContentsMargins(20, 20, 20, 18)
        self.content.setSpacing(16)
        header = dialog_header(tr("Область веб-камеры"), "webcam")
        header.setToolTip(tr("Обведите человека целиком, включая руки. Тяните рамку за углы.\n"
                          "Красная область исключается только из анализа, а не из видео."))
        self.content.addWidget(header)
        self.loading = QLabel(tr("Загрузка кадра…"))
        self.loading.setObjectName("sectionHint")
        self.content.addWidget(self.loading)
        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.setRange(0, 99)
        self.seek.setAccessibleName(tr("Момент видео для выбора веб-камеры"))
        self.seek.setValue(10)
        self.seek.setToolTip(tr("Выбрать другой момент видео"))
        self.seek.sliderReleased.connect(self.load_frame)
        self.content.addWidget(self.seek)
        self.no_camera = QPushButton(tr("В этом исходнике нет веб-камеры"))
        self.no_camera.setProperty("actionRole", "utility")
        self.no_camera.setAutoDefault(False)
        self.no_camera.clicked.connect(lambda: self.done(2))
        self.buttons = DialogButtons(self)
        self.buttons.accepted.connect(self.confirm)
        self.buttons.rejected.connect(self.reject)
        actions = QHBoxLayout()
        actions.addWidget(self.no_camera)
        actions.addStretch()
        actions.addWidget(self.buttons)
        self.content.addLayout(actions)
        self.load_frame()

    def confirm(self) -> None:
        if self.canvas is None:
            return
        if self.canvas.roi.width * self.canvas.roi.height > 0.8:
            self.loading.setText(tr("Оставьте большую часть рабочего изображения вне рамки."))
            self.loading.show()
            return
        self.accept()

    def load_frame(self) -> None:
        if self.canvas is not None:
            self.selection = self.canvas.roi
        self.generation += 1
        generation = self.generation
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.loading.setText(tr("Загрузка кадра…"))
        self.loading.show()
        if (self.seek.value() == 10 and self.initial_frame is not None
                and not self.initial_frame.isNull()):
            self.show_frame(self.initial_frame)
            return
        loader = PreviewLoader(self.source, self.seek.value() / 100)
        _active_loaders.add(loader)
        loader.finished.connect(lambda: _active_loaders.discard(loader))

        def loaded(image: QImage) -> None:
            if generation != self.generation:
                return
            self.show_frame(image)

        def failed(message: str) -> None:
            if generation == self.generation:
                self.loading.setText(tr("Не удалось прочитать кадр: ") + message)

        loader.loaded.connect(loaded)
        loader.failed.connect(failed)
        loader.start()

    def show_frame(self, image: QImage) -> None:
        if self.canvas is not None:
            self.content.removeWidget(self.canvas)
            self.canvas.deleteLater()
        self.canvas = CropCanvas(QPixmap.fromImage(image), self.selection, 0)
        self.content.insertWidget(2, self.canvas, alignment=Qt.AlignmentFlag.AlignCenter)
        self.loading.hide()
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)

    def done(self, result: int) -> None:
        self.generation += 1
        super().done(result)

    @property
    def roi(self) -> Roi:
        if self.canvas is None:
            raise ValueError("The source frame is still loading")
        return self.canvas.roi
