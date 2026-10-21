import os
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QFileDialog
from test_timeline import make_source

from sculptcut.domain import Project, ProjectSettings
from sculptcut.project import save_project
from sculptcut.ui import MainWindow


@pytest.mark.parametrize("preset", ["timelapse", "speed"])
def test_restore_duration_and_latest_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preset: str
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    project = Project(
        sources=[make_source(0, 0, 600_000_000)],
        settings=ProjectSettings.model_validate(
            {"preset": preset, "target_seconds": 420, "music_selection_mode": "full_tracks"}
        ),
    )
    save_project(tmp_path, project)
    opened: list[Path] = []
    monkeypatch.setattr(window, "open_video", lambda path, *a, **kw: opened.append(path))
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a: str(tmp_path))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a: ("", ""))
    old = tmp_path / "renders" / "old.mp4"
    latest = tmp_path / "renders" / "latest.mp4"
    preview = tmp_path / "previews" / "newer-preview.mp4"
    for timestamp, path in enumerate((old, latest, preview), 1000):
        path.parent.mkdir(exist_ok=True)
        path.touch()
        os.utime(path, (timestamp, timestamp))
    try:
        window.target.setValue(12)
        window.preset.setCurrentIndex(window.preset.findData("shorts"))
        assert window.target.isEnabled()
        assert window.target.value() == 60
        window.choose_folder()
        assert window.music_selection.currentData() == "full_tracks"
        assert window.target.value() == 7
        assert window.target.isEnabled()
        assert window.normal_minutes == 7
        assert opened[-1] == latest
        assert window.last_result_video == latest.resolve()
        old.unlink()
        latest.unlink()
        window.choose_folder()
        assert opened[-1] == preview
        preview.unlink()
        window.choose_folder()
        assert window.last_result_video is None
        assert opened == [latest, preview]
        assert "готовый ролик" in window.video.text()
    finally:
        window.close()
        app.processEvents()
