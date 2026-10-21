import os
import shutil
import time
from pathlib import Path
from threading import Event

import pytest
from PySide6.QtWidgets import QApplication

from sculptcut.domain import ProjectSettings
from sculptcut.media import Cancelled, discover, run_process
from sculptcut.preflight import check_creation, writable_directory
from sculptcut.project import cache_get, cache_put, create_project
from sculptcut.ui import MainWindow


def video(path: Path) -> None:
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 "color=s=160x120:r=30:d=2", "-c:v", "libx264", str(path)])


def test_preflight_real_inputs_and_no_project_side_effects(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    video(source)
    music = tmp_path / "music"
    music.mkdir()
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 "sine=frequency=440:duration=2", str(music / "track.wav")])
    root = tmp_path / "future" / "project"
    stages: list[str] = []
    report = check_creation(root, [source], ProjectSettings(
        preset="speed", target_seconds=1, music_folder=music,
    ), stage=stages.append)
    assert report.source_count == report.music_count == 1
    assert report.free_bytes >= report.required_bytes > 0
    assert len(stages) == 3
    assert not root.parent.exists()
    assert not list(tmp_path.glob(".sculptcut-write-check-*"))
    with pytest.raises(ValueError, match="музыкой"):
        check_creation(root, [source], ProjectSettings(preset="music_shorts"))
    assert not root.exists()
    cancel = Event()
    cancel.set()
    with pytest.raises(Cancelled):
        check_creation(root, [source], ProjectSettings(), cancel)
    assert not root.exists()


@pytest.mark.parametrize("case", ["missing", "broken", "empty_music", "broken_music"])
def test_bad_media_rejected_before_creation(tmp_path: Path, case: str) -> None:
    source = tmp_path / "source.mp4"
    video(source)
    music = tmp_path / "music"
    music.mkdir()
    settings = ProjectSettings(preset="speed")
    if case == "missing":
        source.unlink()
    elif case == "broken":
        source.write_bytes(b"This is not video")
    else:
        settings = settings.model_copy(update={"music_folder": music})
        if case == "broken_music":
            (music / "wrong.wav").write_bytes(b"This is not music")
    with pytest.raises((ValueError, RuntimeError)):
        check_creation(tmp_path / "project", [source], settings)
    assert not (tmp_path / "project").exists()


def test_changed_source_keeps_existing_project_and_cache(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    video(source)
    root = tmp_path / "project"
    project = create_project(root, [source])
    sid = project.sources[0].id
    cache_put(root, "test", sid, "key", "retained")
    before = (root / "project.json").read_bytes()
    os.utime(source, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns + 10000000))
    with pytest.raises(ValueError, match="изменилось"):
        check_creation(root, [source], ProjectSettings())
    assert (root / "project.json").read_bytes() == before
    assert cache_get(root, "test", sid, "key") == "retained"


def test_destination_access_and_nonempty_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.mp4"
    video(source)
    with pytest.raises(ValueError, match="пустую папку"):
        check_creation(tmp_path, [source], ProjectSettings())
    with pytest.raises(ValueError, match="выбран файл"):
        check_creation(source, [source], ProjectSettings())

    def denied(*args: object, **kwargs: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr("sculptcut.preflight.tempfile.TemporaryFile", denied)
    with pytest.raises(ValueError, match="Нет доступа"):
        writable_directory(tmp_path / "new-project")
    assert not (tmp_path / "new-project").exists()


@pytest.mark.parametrize("preset,frame", [("timelapse", "viewport"), ("shorts", "square")])
def test_gui_low_disk_blocks_frames_and_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preset: str, frame: str,
) -> None:
    app = QApplication.instance() or QApplication([])
    source = tmp_path / "source.mp4"
    video(source)
    root = tmp_path / "project"
    usage = shutil.disk_usage(tmp_path)
    monkeypatch.setattr("sculptcut.preflight.shutil.disk_usage", lambda _: usage._replace(free=1))
    window = MainWindow()

    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("Invalid preflight must block frames, proxies, and analysis")

    monkeypatch.setattr(window, "choose_crop_area", unexpected)
    monkeypatch.setattr(window, "choose_square_area", unexpected)
    monkeypatch.setattr("sculptcut.ui.make_proxies", unexpected)
    monkeypatch.setattr("sculptcut.ui.extract_features", unexpected)
    try:
        window.show()
        window.folder.setText(str(root))
        window.set_sources([source])
        window.output_name.setText("checked")
        window.preset.setCurrentIndex(window.preset.findData(preset))
        if frame == "viewport":
            window.frame_mode.setCurrentIndex(window.frame_mode.findData(frame))
        else:
            window.shorts_layout.setCurrentIndex(window.shorts_layout.findData(frame))
        window.create_button.click()
        assert window.job is not None
        assert window.job.objectName() == "preflight"
        deadline = time.monotonic() + 20
        while window.job and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert window.job is None
        assert "Недостаточно свободного места" in window.last_error
        assert window.create_button.isEnabled()
        assert not root.exists()
    finally:
        if window.job:
            window.job.cancel.set()
            window.job.wait(20000)
            app.processEvents()
        window.close()
        app.processEvents()
