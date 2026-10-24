"""Small vector-like interface icons drawn at high resolution for Qt buttons."""

from math import cos, pi, sin
from typing import Literal

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

IconName = Literal[
    "music", "image", "folder", "portrait", "portrait_music",
    "drawing", "drawing_music", "timelapse", "speed", "layout_portrait", "layout_square",
    "music_fragments", "music_tracks", "webcam", "undo", "redo", "previous", "next",
    "screen", "viewport", "play", "pause", "cut", "add", "close", "chevron_down", "chevron_up",
    "check", "info",
    "trash", "crop", "episodes_music", "feedback", "presentation", "speaker", "muted",
    "templates", "update",
]


def ui_icon(name: IconName, accent: str = "#a9cbff") -> QIcon:
    """Return a crisp, theme-colored icon without relying on font glyphs."""
    pixmap = QPixmap(48, 48)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(2, 2)
    color = QColor(accent)
    painter.setPen(
        QPen(
            color,
            1.8,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
    )
    painter.setBrush(Qt.BrushStyle.NoBrush)

    if name == "update":
        painter.drawArc(QRectF(3, 3, 18, 18), 45 * 16, 275 * 16)
        painter.drawLine(QPointF(18.5, 3.5), QPointF(18.5, 9))
        painter.drawLine(QPointF(13, 9), QPointF(18.5, 9))
    elif name == "templates":
        painter.drawRoundedRect(QRectF(7, 2, 14, 16), 2, 2)
        painter.setBrush(QColor("#202832"))
        painter.drawRoundedRect(QRectF(3, 6, 14, 16), 2, 2)
        for line_y in (11, 15, 19):
            painter.drawLine(QPointF(6, line_y), QPointF(13, line_y))
    elif name == "presentation":
        painter.drawRoundedRect(QRectF(2, 3, 20, 18), 2.5, 2.5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        for x in (4, 18.5):
            for y in (5.5, 10.5, 15.5):
                painter.drawRoundedRect(QRectF(x, y, 1.5, 2.5), .4, .4)
        star = QPainterPath()
        for index in range(10):
            angle = -pi / 2 + index * pi / 5
            radius = 5.2 if index % 2 == 0 else 2.3
            point = QPointF(12 + cos(angle) * radius, 12 + sin(angle) * radius)
            if index:
                star.lineTo(point)
            else:
                star.moveTo(point)
        star.closeSubpath()
        painter.drawPath(star)
    elif name in ("speaker", "muted"):
        speaker = QPainterPath(QPointF(3, 9))
        for x, y in ((7, 9), (12, 5), (12, 19), (7, 15), (3, 15)):
            speaker.lineTo(QPointF(x, y))
        speaker.closeSubpath()
        painter.drawPath(speaker)
        if name == "muted":
            painter.drawLine(QPointF(16, 9), QPointF(22, 15))
            painter.drawLine(QPointF(16, 15), QPointF(22, 9))
        else:
            waves = QPainterPath(QPointF(16, 8))
            waves.cubicTo(19, 10, 19, 14, 16, 16)
            waves.moveTo(19, 5)
            waves.cubicTo(24, 9, 24, 15, 19, 19)
            painter.drawPath(waves)
    elif name == "feedback":
        bubble = QPainterPath(QPointF(5, 3))
        bubble.lineTo(QPointF(19, 3))
        bubble.quadTo(22, 3, 22, 6)
        bubble.lineTo(QPointF(22, 14))
        bubble.quadTo(22, 17, 19, 17)
        bubble.lineTo(QPointF(10, 17))
        bubble.lineTo(QPointF(5, 21))
        bubble.lineTo(QPointF(5, 17))
        bubble.quadTo(2, 17, 2, 14)
        bubble.lineTo(QPointF(2, 6))
        bubble.quadTo(2, 3, 5, 3)
        painter.drawPath(bubble)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        for x in (7, 12, 17):
            painter.drawEllipse(QRectF(x - 1, 9, 2, 2))
    elif name == "trash":
        painter.drawLine(QPointF(4, 6), QPointF(20, 6))
        painter.drawRoundedRect(QRectF(9, 2, 6, 4), 1, 1)
        bin_shape = QPainterPath(QPointF(6, 6))
        bin_shape.lineTo(QPointF(7, 21))
        bin_shape.lineTo(QPointF(17, 21))
        bin_shape.lineTo(QPointF(18, 6))
        painter.drawPath(bin_shape)
        painter.drawLine(QPointF(10, 10), QPointF(10, 17))
        painter.drawLine(QPointF(14, 10), QPointF(14, 17))
    elif name == "info":
        painter.setBrush(QColor("#344457"))
        painter.drawEllipse(QRectF(2, 2, 20, 20))
        painter.drawLine(QPointF(12, 11), QPointF(12, 17))
        painter.drawLine(QPointF(10, 17), QPointF(14, 17))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QRectF(11, 6, 2, 2))
    elif name in ("previous", "next"):
        if name == "next":
            painter.translate(24, 0)
            painter.scale(-1, 1)
        painter.drawLine(QPointF(5, 5), QPointF(5, 19))
        arrow = QPainterPath(QPointF(18, 5))
        arrow.lineTo(QPointF(8, 12))
        arrow.lineTo(QPointF(18, 19))
        arrow.closeSubpath()
        painter.drawPath(arrow)
    elif name in ("undo", "redo"):
        if name == "redo":
            painter.translate(24, 0)
            painter.scale(-1, 1)
        path = QPainterPath(QPointF(5, 9))
        path.cubicTo(18, 5, 24, 17, 11, 20)
        painter.drawPath(path)
        painter.drawLine(QPointF(5, 9), QPointF(6, 3))
        painter.drawLine(QPointF(5, 9), QPointF(11, 12))
    elif name == "webcam":
        painter.drawRoundedRect(QRectF(3, 2, 18, 14), 4, 4)
        painter.drawEllipse(QRectF(8, 5, 8, 8))
        painter.drawEllipse(QRectF(17, 4, 1, 1))
        painter.drawLine(QPointF(12, 16), QPointF(12, 21))
        painter.drawLine(QPointF(6, 22), QPointF(18, 22))
    elif name == "add":
        painter.drawLine(QPointF(12, 4), QPointF(12, 20))
        painter.drawLine(QPointF(4, 12), QPointF(20, 12))
    elif name == "close":
        painter.drawLine(QPointF(6, 6), QPointF(18, 18))
        painter.drawLine(QPointF(18, 6), QPointF(6, 18))
    elif name in ("chevron_down", "chevron_up"):
        if name == "chevron_up":
            painter.translate(0, 24)
            painter.scale(1, -1)
        arrow = QPainterPath(QPointF(5, 9))
        arrow.lineTo(QPointF(12, 16))
        arrow.lineTo(QPointF(19, 9))
        painter.drawPath(arrow)
    elif name == "check":
        tick = QPainterPath(QPointF(4, 12))
        tick.lineTo(QPointF(9, 17))
        tick.lineTo(QPointF(20, 6))
        painter.drawPath(tick)
    elif name == "play":
        triangle = QPainterPath(QPointF(7, 4))
        triangle.lineTo(QPointF(20, 12))
        triangle.lineTo(QPointF(7, 20))
        triangle.closeSubpath()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawPath(triangle)
    elif name == "pause":
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(QRectF(5, 4, 5, 16), 1, 1)
        painter.drawRoundedRect(QRectF(14, 4, 5, 16), 1, 1)
    elif name == "cut":
        painter.drawEllipse(QRectF(2, 3, 6, 6))
        painter.drawEllipse(QRectF(2, 15, 6, 6))
        painter.drawLine(QPointF(7.5, 7.5), QPointF(21, 20))
        painter.drawLine(QPointF(7.5, 16.5), QPointF(21, 4))
        painter.drawEllipse(QRectF(11, 11, 2, 2))
    elif name == "crop":
        outline = QPainterPath(QPointF(6, 2))
        outline.lineTo(QPointF(6, 18))
        outline.lineTo(QPointF(22, 18))
        painter.drawPath(outline)
        outline = QPainterPath(QPointF(2, 6))
        outline.lineTo(QPointF(18, 6))
        outline.lineTo(QPointF(18, 22))
        painter.drawPath(outline)
    elif name in ("screen", "viewport"):
        painter.drawRoundedRect(QRectF(1, 4, 22, 16), 2, 2)
        if name == "screen":
            painter.drawLine(QPointF(1, 8), QPointF(23, 8))
            painter.drawLine(QPointF(17, 8), QPointF(17, 20))
        else:
            painter.drawLine(QPointF(6, 9), QPointF(10, 9))
            painter.drawLine(QPointF(6, 9), QPointF(6, 13))
            painter.drawLine(QPointF(18, 16), QPointF(14, 16))
            painter.drawLine(QPointF(18, 16), QPointF(18, 12))
    elif name == "music_fragments":
        for x, top, bottom in ((3, 9, 15), (7, 5, 19), (12, 2, 22), (17, 6, 18), (21, 9, 15)):
            painter.drawLine(QPointF(x, top), QPointF(x, bottom))
        painter.drawRoundedRect(QRectF(9, 1, 6, 22), 1, 1)
    elif name == "music_tracks":
        for y in (5, 11, 17):
            painter.drawEllipse(QRectF(2, y, 2, 2))
            painter.drawLine(QPointF(7, y+1), QPointF(15, y+1))
        painter.drawLine(QPointF(20, 5), QPointF(20, 20))
        painter.drawLine(QPointF(20, 20), QPointF(17, 17))
        painter.drawLine(QPointF(20, 20), QPointF(23, 17))
    elif name in ("layout_portrait", "layout_square"):
        painter.drawRoundedRect(QRectF(5, 1, 14, 22), 2, 2)
        if name == "layout_square":
            painter.setBrush(QColor("#38587b"))
            painter.drawRect(QRectF(5, 5, 14, 14))
        else:
            painter.drawLine(QPointF(8, 5), QPointF(11, 5))
            painter.drawLine(QPointF(8, 5), QPointF(8, 9))
            painter.drawLine(QPointF(16, 19), QPointF(13, 19))
            painter.drawLine(QPointF(16, 19), QPointF(16, 15))
    elif name == "episodes_music":
        painter.drawRoundedRect(QRectF(2, 3, 15, 18), 2, 2)
        for x in (5, 10, 14):
            painter.drawPoint(QPointF(x, 6))
            painter.drawPoint(QPointF(x, 18))
        triangle = QPainterPath(QPointF(7, 9))
        triangle.lineTo(QPointF(12, 12))
        triangle.lineTo(QPointF(7, 15))
        triangle.closeSubpath()
        painter.drawPath(triangle)
        painter.setBrush(color)
        painter.drawEllipse(QRectF(17, 16, 5, 4))
        painter.drawLine(QPointF(22, 18), QPointF(22, 8))
        painter.drawLine(QPointF(22, 8), QPointF(19, 9))
    elif name in ("portrait", "portrait_music", "drawing", "drawing_music"):
        painter.drawRoundedRect(QRectF(4, 2, 13, 20), 2, 2)
        if name.startswith("drawing"):
            painter.drawLine(QPointF(7, 16), QPointF(13, 7))
            painter.drawLine(QPointF(9, 17), QPointF(15, 8))
            painter.drawLine(QPointF(7, 16), QPointF(9, 17))
        else:
            play = QPainterPath(QPointF(8, 8))
            play.lineTo(QPointF(8, 16))
            play.lineTo(QPointF(13, 12))
            play.closeSubpath()
            painter.drawPath(play)
        if name.endswith("music"):
            painter.setBrush(QColor("#202936"))
            painter.drawRoundedRect(QRectF(14, 11, 9, 12), 2, 2)
            painter.setBrush(color)
            painter.drawEllipse(QRectF(16, 18, 4, 3))
            painter.drawLine(QPointF(20, 19), QPointF(20, 14))
            painter.drawLine(QPointF(20, 14), QPointF(22, 15))
    elif name == "timelapse":
        painter.drawEllipse(QRectF(2, 2, 20, 20))
        painter.drawLine(QPointF(12, 6), QPointF(12, 12))
        painter.drawLine(QPointF(12, 12), QPointF(17, 15))
        painter.drawLine(QPointF(4, 5), QPointF(4, 1))
    elif name == "speed":
        for x in (3, 12):
            arrow = QPainterPath(QPointF(x, 5))
            arrow.lineTo(QPointF(x+8, 12))
            arrow.lineTo(QPointF(x, 19))
            painter.drawPath(arrow)
    elif name == "music":
        painter.setBrush(color)
        painter.drawEllipse(QRectF(3, 15.5, 5.5, 4.5))
        painter.drawEllipse(QRectF(14, 12.5, 5.5, 4.5))
        painter.drawLine(QPointF(8, 17.5), QPointF(8, 5))
        painter.drawLine(QPointF(19.5, 14.5), QPointF(19.5, 2.5))
        painter.drawLine(QPointF(8, 5), QPointF(19.5, 2.5))
    elif name == "image":
        painter.drawRoundedRect(QRectF(2, 3, 20, 18), 2, 2)
        painter.drawEllipse(QRectF(15, 6, 3, 3))
        mountain = QPainterPath(QPointF(4, 17))
        mountain.lineTo(QPointF(9, 11))
        mountain.lineTo(QPointF(12.5, 14.5))
        mountain.lineTo(QPointF(15, 12))
        mountain.lineTo(QPointF(20, 17))
        painter.drawPath(mountain)
    else:
        folder = QPainterPath(QPointF(2, 7))
        folder.lineTo(QPointF(2, 19))
        folder.quadTo(QPointF(2, 21), QPointF(4, 21))
        folder.lineTo(QPointF(20, 21))
        folder.quadTo(QPointF(22, 21), QPointF(22, 19))
        folder.lineTo(QPointF(22, 8))
        folder.quadTo(QPointF(22, 6), QPointF(20, 6))
        folder.lineTo(QPointF(11, 6))
        folder.lineTo(QPointF(9, 3))
        folder.lineTo(QPointF(4, 3))
        folder.quadTo(QPointF(2, 3), QPointF(2, 5))
        folder.closeSubpath()
        painter.drawPath(folder)
        painter.setBrush(color)
        play = QPainterPath(QPointF(10, 10))
        play.lineTo(QPointF(10, 17))
        play.lineTo(QPointF(16, 13.5))
        play.closeSubpath()
        painter.drawPath(play)

    painter.end()
    pixmap.setDevicePixelRatio(2)
    return QIcon(pixmap)
