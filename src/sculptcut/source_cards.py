"""Source cards and asynchronous metadata for the desktop video list."""

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import (
    QModelIndex,
    QPersistentModelIndex,
    QRect,
    QSize,
    Qt,
    QThread,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QIcon,
    QImage,
    QPainter,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QLabel,
    QListWidget,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from sculptcut.crop_dialog import preview_frame
from sculptcut.i18n import tr
from sculptcut.media import probe
from sculptcut.time_format import format_time
from sculptcut.ui_icons import ui_icon

META_ROLE = Qt.ItemDataRole.UserRole + 1
PRESENTATION_ROLE = Qt.ItemDataRole.UserRole + 2
VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".webm"}


def dropped_video_paths(urls: list[QUrl]) -> list[Path]:
    """Accept only local video files; never treat a remote URL as a source."""
    return [
        Path(url.toLocalFile())
        for url in urls
        if url.isLocalFile()
        and Path(url.toLocalFile()).is_file()
        and Path(url.toLocalFile()).suffix.lower() in VIDEO_SUFFIXES
    ]


class VideoDropZone(QFrame):
    files_dropped = Signal(object)
    choose_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("videoDropZone")
        self.setAcceptDrops(True)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(10)
        icon = QLabel()
        icon.setPixmap(ui_icon("add", "#76b4f4").pixmap(QSize(40, 40)))
        icon.setObjectName("dropIcon")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon)
        title = QLabel(tr("Перетащите видео сюда"))
        title.setObjectName("dropTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setWordWrap(True)
        layout.addWidget(title)
        hint = QLabel(tr("MP4, MKV, MOV или WebM · файлы остаются на вашем диске"))
        hint.setObjectName("dropHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        choose = QPushButton(tr("Выбрать файлы…"))
        choose.setObjectName("dropChoose")
        choose.clicked.connect(self.choose_clicked)
        layout.addWidget(choose, alignment=Qt.AlignmentFlag.AlignHCenter)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if dropped_video_paths(event.mimeData().urls()):
            event.acceptProposedAction()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if dropped_video_paths(event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = dropped_video_paths(event.mimeData().urls())
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()


class SourceCards(QListWidget):
    files_dropped = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDropIndicatorShown(True)
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setUniformItemSizes(True)
        self.setItemDelegate(SourceCardDelegate(self))
        self.setSpacing(4)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if dropped_video_paths(event.mimeData().urls()):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if dropped_video_paths(event.mimeData().urls()):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        if event.source() is self:
            super().dropEvent(event)
            return
        paths = dropped_video_paths(event.mimeData().urls())
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


class SourceCardDelegate(QStyledItemDelegate):
    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        painter.save()
        item_rect = option.rect  # type: ignore[attr-defined]
        rect = item_rect.adjusted(2, 2, -2, -2)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)  # type: ignore[attr-defined]
        presentation = str(index.data(PRESENTATION_ROLE) or "")
        painter.setPen(QColor("#e8b35c") if presentation else
                       QColor("#5495f5") if selected else QColor("#354354"))
        painter.setBrush(QColor("#213451") if selected else QColor("#1d2631"))
        painter.drawRoundedRect(rect, 8, 8)
        thumb = QRect(rect.left() + 10, rect.top() + 10, 96, 58)
        painter.fillRect(thumb, QColor("#0d131b"))
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(icon, QIcon) and not icon.isNull():
            icon.paint(painter, thumb)
        title_rect = QRect(thumb.right() + 12, rect.top() + 12, rect.width() - 130, 23)
        painter.setPen(QColor("#f2f6fc"))
        title = painter.fontMetrics().elidedText(
            str(index.data(Qt.ItemDataRole.DisplayRole) or ""),
            Qt.TextElideMode.ElideMiddle,
            title_rect.width(),
        )
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignVCenter, title)
        meta_rect = QRect(title_rect.left(), title_rect.bottom() + 3, rect.width() - 130, 20)
        if presentation:
            font = painter.font()
            font.setPixelSize(11)
            painter.setFont(font)
            badge = QRect(meta_rect.left(), meta_rect.top(),
                          min(meta_rect.width(), painter.fontMetrics().horizontalAdvance(
                              tr("Презентация")) + 12), 18)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#40372a"))
            painter.drawRoundedRect(badge, 4, 4)
            painter.setPen(QColor("#efc681"))
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, tr("Презентация"))
            meta_rect.translate(0, 20)
            meta_rect.setHeight(14)
        painter.setPen(QColor("#afbdcf") if presentation else QColor("#9aabc0"))
        meta = painter.fontMetrics().elidedText(
            tr(presentation or str(index.data(META_ROLE) or "Сведения загружаются…")),
            Qt.TextElideMode.ElideRight,
            meta_rect.width(),
        )
        painter.drawText(meta_rect, Qt.AlignmentFlag.AlignVCenter, meta)
        painter.restore()

    def sizeHint(
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        return QSize(0, 82)


class SourceMediaLoader(QThread):
    loaded = Signal(str, object, str)

    def __init__(self, paths: list[Path]) -> None:
        super().__init__()
        self.paths = paths

    def run(self) -> None:
        for path in self.paths:
            if self.isInterruptionRequested():
                break
            try:
                info = probe(path)
                duration = format_time(round((info.duration_us or 0) / 1_000_000))
                fps = info.fps_num / info.fps_den
                detail = f"{duration}  ·  {info.width}×{info.height}  ·  {fps:g} fps"
            except (OSError, ValueError, RuntimeError):
                detail = tr("Сведения недоступны")
            try:
                frame = preview_frame(path)
            except (OSError, ValueError, RuntimeError):
                frame = QImage()
            self.loaded.emit(str(path), frame, detail)


_active_source_loaders: set[SourceMediaLoader] = set()


def load_source_media(paths: list[Path], on_loaded: Callable[[str, QImage, str], None]) -> None:
    if not paths:
        return
    loader = SourceMediaLoader(paths)
    loader.loaded.connect(on_loaded)
    loader.finished.connect(lambda: _active_source_loaders.discard(loader))
    _active_source_loaders.add(loader)
    loader.start()
