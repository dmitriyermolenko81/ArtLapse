"""A seekable montage ruler whose blocks retain their actual output durations."""

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent
from PySide6.QtWidgets import QSlider


class MontageTimeline(QSlider):
    def __init__(self) -> None:
        super().__init__(Qt.Orientation.Horizontal)
        self.blocks: list[tuple[int, int, int]] = []
        self.setMinimumHeight(48)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#111923"))
        scale = self.width() / max(1, self.maximum())
        for row, start, end in self.blocks:
            a, b = round(start * scale), round(end * scale)
            painter.fillRect(a, 4, max(1, b - a - 1), 28, QColor("#476ca7"))
            if b - a > 24:
                painter.setPen(QColor("#ffffff"))
                painter.drawText(a + 4, 23, str(row + 1))
        painter.setPen(QColor("#f5ce80"))
        x = round(self.value() * scale)
        painter.drawLine(x, 0, x, self.height())

    def seek_at(self, event: QMouseEvent) -> None:
        value = round(event.position().x() / max(1, self.width()) * self.maximum())
        with QSignalBlocker(self):
            self.setValue(max(self.minimum(), min(self.maximum(), value)))
        self.sliderMoved.emit(self.value())

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.setSliderDown(True)
            self.seek_at(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.isSliderDown():
            self.seek_at(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self.setSliderDown(False)
