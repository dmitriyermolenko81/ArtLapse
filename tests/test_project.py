import os
import sqlite3
from pathlib import Path

import pytest

from sculptcut.domain import Project
from sculptcut.media import MediaError, discover, run_process
from sculptcut.project import (
    atomic_write,
    cache_get,
    cache_put,
    cache_session,
    create_project,
    load_project,
    validate_sources,
)


def test_project_roundtrip_invalidation(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=s=64x64:d=1",
            "-c:v",
            "libx264",
            str(source),
        ]
    )
    root = tmp_path / "project"
    project = create_project(root, [source])
    assert load_project(root) == project
    assert (root / "analysis.sqlite").is_file()
    cache_put(root, "test", "source-0000", "v1", {"done": True})
    assert cache_get(root, "test", "source-0000", "v1") == {"done": True}
    assert cache_get(root, "test", "source-0000", "v2") is None
    validate_sources(root, project)
    os.utime(source, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns + 100000))
    with pytest.raises(MediaError, match="reimport"):
        validate_sources(root, project)
    assert cache_get(root, "test", "source-0000", "v1") is None
    with pytest.raises(ValueError, match="empty"):
        create_project(root, [source])


def test_atomic_failure_preserves_previous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = tmp_path / "project.json"
    destination.write_text("previous")

    def fail(source: object, destination: object) -> None:
        raise OSError("injected disk failure")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="disk failure"):
        atomic_write(destination, "new")
    assert destination.read_text() == "previous"
    assert list(tmp_path.iterdir()) == [destination]


def test_future_schema_rejected(tmp_path: Path) -> None:
    (tmp_path / "project.json").write_text('{"schema_version": 99}')
    with pytest.raises(ValueError, match="Unsupported"):
        load_project(tmp_path)
    assert Project(sources=[]).schema_version == 1


def test_scoped_cache_commits_completed_windows_and_closes_on_cancellation(tmp_path: Path) -> None:
    from sculptcut.media import Cancelled

    with pytest.raises(Cancelled):
        with cache_session(tmp_path) as cache:
            cache.put("windows", "a", "v1", {"ranges": []})
            assert cache_get(tmp_path, "windows", "a", "v1") == {"ranges": []}
            assert cache.get("windows", "a", "v2") is None
            cache.put("windows", "a", "v2", {"ranges": [1]})
            assert cache.get("windows", "a", "v1") is None
            # Invalidation by another operation must be immediately visible.
            cache_put(tmp_path, "windows", "a", "v3", {"ranges": [2]})
            assert cache.get("windows", "a", "v2") is None
            assert cache.get("windows", "a", "v3") == {"ranges": [2]}
            raise Cancelled("cancelled after completed window")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        cache.get("windows", "a", "v3")
    assert cache_get(tmp_path, "windows", "a", "v3") == {"ranges": [2]}
