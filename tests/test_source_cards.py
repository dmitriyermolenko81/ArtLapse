"""Integration checks for the source cards and file drop surface."""

import shutil
from pathlib import Path
from time import monotonic

import pytest
from PySide6.QtCore import Qt, QUrl
from PySide6.QtWidgets import QApplication

from sculptcut.media import discover, run_process
from sculptcut.source_cards import META_ROLE, dropped_video_paths
from sculptcut.ui import MainWindow


@pytest.mark.parametrize("suffix", [".MP4", ".WEBM"])
def test_drop_accepts_existing_local_videos_only(tmp_path: Path, suffix: str) -> None:
    video = tmp_path / f"скульптинг{suffix}"
    video.write_bytes(b"video")
    text_file = tmp_path / "notes.txt"
    text_file.write_text("notes")
    urls = [
        QUrl("https://example.com/remote.mp4"),
        QUrl.fromLocalFile(str(text_file)),
        QUrl.fromLocalFile(str(tmp_path / "missing.mp4")),
        QUrl.fromLocalFile(str(video)),
    ]
    assert dropped_video_paths(urls) == [video]


def test_left_drop_zone_populates_thumbnail_without_replacing_final_view(tmp_path: Path) -> None:
    source = tmp_path / "кадр с пробелом.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=160x90:r=10:d=2",
            "-c:v",
            "libx264",
            str(source),
        ]
    )
    original = source.stat().st_size
    finished = tmp_path / "готовый.mp4"
    shutil.copyfile(source, finished)
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.show()
        app.processEvents()
        window.folder.setText(str(tmp_path / "project"))
        assert window.drop_zone.isVisible()
        window.open_video(finished, "Готовый ролик")
        window.drop_zone.files_dropped.emit([source])
        assert window.source_paths() == [source]
        assert not window.drop_zone.isVisible()
        assert Path(window.player.source().toLocalFile()) == finished
        deadline = monotonic() + 15
        while monotonic() < deadline:
            app.processEvents()
            item = window.sources.item(0)
            if "00:02" in item.data(META_ROLE) and not item.icon().isNull():
                break
        item = window.sources.item(0)
        assert "00:02" in item.data(META_ROLE)
        assert not item.icon().isNull()
        assert item.data(Qt.ItemDataRole.UserRole) == str(source)
        assert source.stat().st_size == original
        assert window.source_stack.currentWidget() is window.sources
        window.sources.setCurrentRow(0)
        assert Path(window.player.source().toLocalFile()) == finished
        window.sources.selectAll()
        window.remove_sources()
        assert window.drop_zone.isVisible()
        assert Path(window.player.source().toLocalFile()) == finished
    finally:
        window.close()
        app.processEvents()
