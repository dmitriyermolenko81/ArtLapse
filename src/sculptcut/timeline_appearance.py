"""Shared quiet lane drawing for music clips and reviewed video scenes."""
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter

from sculptcut.simple_timeline import SimpleTimeline
from sculptcut.time_format import format_time


@dataclass(frozen=True)
class LanePiece:
    row: int
    start_ms: float
    end_ms: float
    title: str
    detail: str
    short_title: str = ''
    short_detail: str = ''


def lane_bounds(track: SimpleTimeline) -> tuple[int, int]:
    if track.compact_lane:
        return (22, track.height()-10) if track.show_ruler else (3, track.height()-3)
    return 28, 92


def paint_lane(p: QPainter, track: SimpleTimeline, pieces: list[LanePiece],
               selected: int, empty_label: str = '',
               decoration: Callable[[QPainter, LanePiece, int, int, int, int], None] | None = None
               ) -> None:
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.fillRect(track.rect(), QColor('#111923'))
    top, bottom = lane_bounds(track)
    height = bottom-top
    if track.lane_label:
        p.setPen(QColor('#b4c3d4'))
        p.drawText(QRectF(3, top, track.left_padding-8, height),
                   Qt.AlignmentFlag.AlignVCenter, track.lane_label)
    if track.compact_lane:
        p.setClipRect(QRectF(track.left_padding-4, 0,
                            track.width()-track.left_padding+4, track.height()))
    p.setPen(QColor('#aab9cb'))
    intervals = min(5, max(1, (track.width()-40)//100))
    for i in range(intervals+1) if track.show_ruler else ():
        ms = track.view_start+track.view_span*i/intervals
        x = round(track.x_at(ms))
        label = format_time(ms/1000, 1 if track.view_span < 30000 else 0)
        label_width = p.fontMetrics().horizontalAdvance(label)
        p.drawText(max(track.left_padding+3 if track.compact_lane else 20,
            min(track.width()-label_width-18, x-label_width//2)), 17, label)
    if not pieces and empty_label:
        p.setPen(QColor('#74879c'))
        p.setBrush(QColor('#1b2632'))
        area = QRectF(track.left_padding, top, track.width()-track.left_padding-10, height)
        if not track.compact_lane:
            area = QRectF(1, 27, track.width()-2, 65)
        radius = 5 if track.compact_lane else 7
        p.drawRoundedRect(area, radius, radius)
        p.setPen(QColor('#d6e5f2'))
        p.drawText(QRectF(track.left_padding, top, track.width()-track.left_padding-10,
            height), Qt.AlignmentFlag.AlignCenter, empty_label)
    for clip in pieces:
        if clip.end_ms < track.view_start or clip.start_ms > track.view_start+track.view_span:
            continue
        a = round(track.x_at(clip.start_ms))
        width = max(2, round(track.x_at(clip.end_ms))-a)
        if width < 8:
            p.fillRect(a, top, width, height,
                       QColor('#2c645f' if clip.row == selected else '#29434e'))
            continue
        p.setPen(QColor('#9accc6' if clip.row == selected else '#446a72'))
        p.setBrush(QColor('#2c645f' if clip.row == selected else '#29434e'))
        radius = 5 if track.compact_lane else 6
        p.drawRoundedRect(QRectF(a+1, top, width-2, height), radius, radius)
        if decoration is not None:
            decoration(p, clip, a, width, top, bottom)
        if width >= 24:
            for edge in (a+5, a+width-7):
                p.fillRect(edge, top+(height- min(24, height-8))//2,
                           2, min(24, height-8), QColor('#bbdfd9'))
        if width > 32:
            text_x = max(track.left_padding+6, a+(12 if track.compact_lane else 16))
            text_width = max(0, min(track.width()-16, a+width-16)-text_x)
            title = clip.short_title if width < 120 and clip.short_title else clip.title
            detail = clip.short_detail if width < 120 and clip.short_detail else clip.detail
            lines = ((title, top+(height+p.fontMetrics().ascent())//2-2, 'white'),) if (
                track.compact_lane) else ((title, 51, 'white'), (detail, 71, '#b4d1d1'))
            for text, y, color in lines:
                p.setPen(QColor(color))
                p.drawText(text_x, y, p.fontMetrics().elidedText(
                    text, Qt.TextElideMode.ElideRight, text_width))
    p.setPen(QColor('#ffd28c'))
    x = round(track.x_at(track.value()))
    p.drawLine(x, 0, x, track.height())
