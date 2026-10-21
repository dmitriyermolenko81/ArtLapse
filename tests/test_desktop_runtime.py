import errno
import io
import json
import os
from pathlib import Path

import pytest

from sculptcut.logging_config import configure_logging
from sculptcut.project import atomic_write


def test_portable_tools_and_state_follow_moved_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    from sculptcut.media import MediaError, discover
    from sculptcut.runtime_paths import data_directory, portable_root

    root = tmp_path / "переносная программа"
    (root / "bin").mkdir(parents=True)
    (root / "portable.txt").write_text("portable", encoding="utf-8")
    (root / "bin/ffmpeg.exe").write_bytes(b"test executable path")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(root / "SculptCut.exe"))
    monkeypatch.delenv("SCULPTCUT_DATA_DIR", raising=False)
    monkeypatch.setenv("SCULPTCUT_FFMPEG", str(tmp_path / "missing.exe"))
    assert portable_root() == root
    assert data_directory() == root / "data"
    assert Path(discover("ffmpeg")) == root / "bin/ffmpeg.exe"
    with pytest.raises(MediaError, match="Portable component missing"):
        discover("ffprobe")
    moved = tmp_path / "другое место"
    root.rename(moved)
    monkeypatch.setattr(sys, "executable", str(moved / "SculptCut.exe"))
    assert data_directory() == moved / "data"
    assert Path(discover("ffmpeg")) == moved / "bin/ffmpeg.exe"
    monkeypatch.setenv("SCULPTCUT_DATA_DIR", str(tmp_path / "diagnostics"))
    assert data_directory() == tmp_path / "diagnostics"


def test_runtime_logs_in_configured_unicode_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "данные приложения"
    monkeypatch.setenv("SCULPTCUT_DATA_DIR", str(root))
    logger = configure_logging(io.StringIO())
    logger.info("Запуск", extra={"event": "startup"})
    path = root / "logs" / f"sculptcut-{os.getpid()}.jsonl"
    assert json.loads(path.read_text(encoding="utf-8"))["event"] == "startup"
    monkeypatch.delenv("SCULPTCUT_DATA_DIR")
    configure_logging(io.StringIO())


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EACCES])
def test_atomic_publish_failure_preserves_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    destination = tmp_path / "project.json"
    destination.write_text("original", encoding="utf-8")

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError(code, "simulated storage failure")

    monkeypatch.setattr("sculptcut.project.os.replace", fail)
    with pytest.raises(OSError) as error:
        atomic_write(destination, "updated")
    assert error.value.errno == code
    assert destination.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.iterdir()) == [destination]
