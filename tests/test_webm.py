"""Real WebM ingestion, thumbnails, proxy/analysis and MP4 export."""

import hashlib
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from sculptcut.cli import main
from sculptcut.crop_dialog import preview_frame
from sculptcut.media import discover, probe, run_process
from sculptcut.rendering import verify_output
from sculptcut.ui import MainWindow


@pytest.mark.parametrize("codec", ["libvpx", "libvpx-vp9"])
def test_webm_to_mp4(tmp_path: Path, codec: str) -> None:
    source = tmp_path / "запись с пробелом.WEBM"
    run_process([
        discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
        "testsrc2=s=320x180:r=30:d=3", "-f", "lavfi", "-i",
        "sine=frequency=440:duration=3", "-c:v", codec, "-deadline", "realtime",
        "-c:a", "libopus", "-shortest", str(source),
    ])
    digest = hashlib.sha256(source.read_bytes()).digest()
    info = probe(source)
    assert (info.width, info.height) == (320, 180)
    assert info.duration_us == 3_000_000
    assert info.audio_streams
    assert not preview_frame(source).isNull()
    output = tmp_path / "готовый.mp4"
    assert main([
        "run", str(tmp_path / "проект"), str(source),
        "--target-seconds", "1", "--output", str(output),
    ]) == 0
    verify_output(output, 30, 30)
    run_process([discover("ffmpeg"), "-v", "error", "-xerror", "-i",
                 str(output), "-f", "null", "-"])
    assert hashlib.sha256(source.read_bytes()).digest() == digest


def test_webm_file_picker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.webm"
    chosen: list[str] = []

    def choose(*args: object) -> tuple[list[str], str]:
        chosen.append(str(args[3]))
        return [str(source)], ""

    monkeypatch.setattr("sculptcut.ui.QFileDialog.getOpenFileNames", choose)
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.show()
        app.processEvents()
        window.folder.setText(str(tmp_path / "project"))
        window.add_videos()
        assert "*.webm" in chosen[0]
        assert window.source_paths() == [source]
    finally:
        window.close()
        app.processEvents()
