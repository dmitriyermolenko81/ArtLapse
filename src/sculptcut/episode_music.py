"""A compact output-time music envelope and edit boundaries."""

from pathlib import Path
from threading import Event

import numpy as np
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent, QWheelEvent
from PySide6.QtWidgets import QWidget

from sculptcut.domain import ProjectSettings
from sculptcut.i18n import tr
from sculptcut.intro import output_frame_count
from sculptcut.media import Cancelled
from sculptcut.music_waveform import MusicWave, OutputEpisode, build_music_wave, output_episodes
from sculptcut.planning import RenderPlan
from sculptcut.time_format import format_time


class MusicWorker(QThread):
    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, root: Path, settings: ProjectSettings, plan: RenderPlan) -> None:
        super().__init__()
        self.root, self.settings, self.plan = root, settings, plan
        self.cancel = Event()

    def run(self) -> None:
        try:
            wave = build_music_wave(self.root, self.settings, self.plan, self.cancel)
            if not self.cancel.is_set():
                self.ready.emit(wave)
        except Cancelled:
            pass
        except (OSError, ValueError, RuntimeError) as exc:
            if not self.cancel.is_set():
                self.failed.emit(str(exc))


_workers: set[MusicWorker] = set()


class EpisodeMusic(QWidget):
    episode_requested = Signal(int)

    def __init__(self, root: Path, parent: QWidget) -> None:
        super().__init__(parent)
        self.root = root
        self.setFixedHeight(88)
        self.setMouseTracking(True)
        self.wave: MusicWave | None = None
        self.episodes: list[OutputEpisode] = []
        self.selected = -1
        self.start, self.span, self.duration = 0.0, 1.0, 1.0
        self.status = tr("Музыка не выбрана")
        self.worker: MusicWorker | None = None
        self.pending: tuple[ProjectSettings, RenderPlan] | None = None
        self.signature = ""
        self.closed = False

    def set_plan(self, settings: ProjectSettings, plan: RenderPlan) -> None:
        first = not self.episodes
        self.episodes = output_episodes(settings, plan)
        self.duration = (output_frame_count(settings, plan.output_fps, plan.actual_frame_count)
                         / plan.output_fps)
        self.set_view(0 if first else self.start, self.duration if first else self.span)
        signature = settings.model_dump_json() + f"|{self.duration:.9f}"
        if signature != self.signature:
            self.signature = signature
            self.wave = None
            self.status = (tr("Загрузка музыки…") if settings.music_path or settings.music_folder
                           else tr("Музыка не выбрана"))
            self.pending = ((settings, plan)
                            if settings.music_path or settings.music_folder else None)
            if self.worker is not None:
                self.worker.cancel.set()
            else:
                self.start_worker()
        self.update()

    def start_worker(self) -> None:
        if self.closed or self.pending is None:
            return
        settings, plan = self.pending
        self.pending = None
        worker = MusicWorker(self.root, settings, plan)
        self.worker = worker
        _workers.add(worker)
        worker.ready.connect(self.received)
        worker.failed.connect(self.failed)
        worker.finished.connect(self.worker_finished)
        worker.finished.connect(lambda: _workers.discard(worker))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def received(self, wave: MusicWave | None) -> None:
        if self.closed or self.worker is None or self.worker.cancel.is_set():
            return
        self.wave = wave
        self.status = tr("Музыка не выбрана") if wave is None else ""
        self.update()

    def failed(self, message: str) -> None:
        if not self.closed and self.worker is not None and not self.worker.cancel.is_set():
            self.status = tr("Музыка недоступна")
            self.setToolTip(message)
            self.update()

    def worker_finished(self) -> None:
        self.worker = None
        self.start_worker()

    def shutdown(self) -> None:
        self.closed = True
        self.pending = None
        if self.worker is not None:
            self.worker.cancel.set()
            self.worker.wait(5000)

    def set_view(self, start: float, span: float) -> None:
        self.span = min(self.duration, max(min(1.0, self.duration), span))
        self.start = max(0.0, min(start, self.duration - self.span))
        self.update()

    def select_episode(self, row: int) -> None:
        self.selected = row
        matches = [e for e in self.episodes if e.row == row]
        if matches:
            a, b = matches[0].start, matches[-1].end
            self.set_view(a - 2, max(6, b - a + 4))
        else:
            self.update()

    def x_at(self, seconds: float) -> float:
        return 7 + (seconds - self.start) / self.span * max(1, self.width() - 15)

    def time_at(self, x: float) -> float:
        return self.start + max(0.0, min(1.0, (x - 7) / max(1, self.width() - 15))) * self.span

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#141d28"))
        painter.setPen(QColor("#9aabc0"))
        painter.drawText(7, 14,
                         tr("Музыка · готовый ролик · склейки — голубые, пики — янтарные"))
        if self.status:
            painter.drawText(7, 48, self.status)
            return
        wave = self.wave
        if wave is None:
            return
        for episode in self.episodes:
            if episode.row == self.selected:
                left = max(7, self.x_at(episode.start))
                right = min(self.width()-8, self.x_at(episode.end))
                if right > left:
                    painter.fillRect(round(left), 20, round(right-left), 50, QColor("#223955"))
        painter.setPen(QColor("#7493b4"))
        for x in range(7, self.width() - 8):
            a = max(0, int(self.time_at(x) / wave.step))
            b = min(len(wave.levels), max(a + 1, int(self.time_at(x + 1) / wave.step)))
            level = float(np.max(wave.levels[a:b])) if a < b else 0.0
            height = max(1, round(level * 22))
            painter.drawLine(x, 45 - height, x, 45 + height)
        painter.setPen(QColor("#d5a071"))
        for accent in wave.accents:
            if self.start <= accent <= self.start + self.span:
                x = round(self.x_at(accent))
                painter.drawLine(x, 21, x, 29)
        painter.setPen(QColor("#76b4f4"))
        for episode in self.episodes[1:]:
            if self.start <= episode.start <= self.start + self.span:
                x = round(self.x_at(episode.start))
                painter.drawLine(x, 19, x, 71)
        painter.setPen(QColor("#9aabc0"))
        painter.drawText(7, 84, format_time(self.start, 2))
        label = format_time(self.start + self.span, 2)
        painter.drawText(self.width() - painter.fontMetrics().horizontalAdvance(label) - 8,
                         84, label)
        selected = next((e for e in self.episodes[1:] if e.row == self.selected), None)
        if selected is not None and wave.accents:
            accent = min(wave.accents, key=lambda t: abs(t - selected.start))
            delta = round((accent - selected.start) * 1000)
            text = tr(f"Склейка {format_time(selected.start, 2)} · пик {delta:+d} мс")
            painter.setPen(QColor("#d5a071"))
            painter.drawText((self.width()-painter.fontMetrics().horizontalAdvance(text))//2,
                             84, text)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        seconds = self.time_at(event.position().x())
        cuts = self.episodes[1:]
        nearest = min(cuts, key=lambda e: abs(e.start - seconds), default=None)
        text = format_time(seconds, 2) + tr(" · Колесо — масштаб, Shift+колесо — сдвиг")
        if nearest is not None and abs(self.x_at(nearest.start) - event.position().x()) < 9:
            text = tr("Склейка · ") + format_time(nearest.start, 2)
            if self.wave and self.wave.accents:
                accent = min(self.wave.accents, key=lambda t: abs(t - nearest.start))
                delta = round((accent - nearest.start) * 1000)
                text += tr(f" · ближайший пик {delta:+d} мс")
            text += tr("\nКлик — выбрать эпизод. Пики громкости — ориентир, не распознанные доли.")
        self.setToolTip(text)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or not self.wave:
            return
        time = self.time_at(event.position().x())
        cut = min(self.episodes[1:], key=lambda e: abs(e.start - time), default=None)
        if cut is not None and abs(self.x_at(cut.start) - event.position().x()) <= 7:
            self.episode_requested.emit(cut.row)
            return
        episode = next((e for e in self.episodes if e.start <= time < e.end), None)
        if episode is not None:
            self.episode_requested.emit(episode.row)

    def wheelEvent(self, event: QWheelEvent) -> None:
        steps = event.angleDelta().y() / 120
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.set_view(self.start - steps * self.span * 0.15, self.span)
        else:
            anchor = self.time_at(event.position().x())
            span = self.span / 1.5 ** max(-10, min(10, steps))
            self.set_view(anchor - (anchor-self.start) * span / self.span, span)
        event.accept()
