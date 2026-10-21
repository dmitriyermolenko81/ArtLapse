import hashlib
import os
import shutil
import time
from pathlib import Path
from threading import Event

import pytest
from PySide6.QtWidgets import QApplication, QFileDialog

from sculptcut.domain import Override, Roi
from sculptcut.media import Cancelled, MediaError, discover, run_process
from sculptcut.planning import build_plan
from sculptcut.project import (
    cache_get,
    cache_put,
    create_project,
    load_project,
    relink_sources,
    resolve_snapshot_sources,
    save_project,
    validate_sources,
)
from sculptcut.rendering import render, verify_output
from sculptcut.ui import MainWindow


def video(path: Path, color: str = "blue") -> None:
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 f"color=c={color}:s=160x120:r=30:d=2", "-c:v", "libx264", str(path)])


def test_move_preserves_edit_and_real_export(tmp_path: Path) -> None:
    original = tmp_path / "original.mp4"
    video(original)
    root = tmp_path / "project"
    project = create_project(root, [original])
    sid = project.sources[0].id
    project.rois[sid] = Roi(x=0.1, y=0.1, width=0.6, height=0.6)
    project.overrides.append(Override(source_id=sid, start_us=0, end_us=500000,
                                     action="KEEP", speed=1))
    project.settings.shorts_square_rois[sid] = project.rois[sid]
    save_project(root, project)
    plan = build_plan(project, [], 1)
    cache_put(root, "test", sid, "key", {"kept": True})
    moved = tmp_path / "переехавшее видео.mp4"
    original.rename(moved)
    # A copy to another filesystem may change the timestamp; content remains identical.
    os.utime(moved, ns=(moved.stat().st_atime_ns, moved.stat().st_mtime_ns + 10000000))
    digest = hashlib.sha256(moved.read_bytes()).hexdigest()
    updated = relink_sources(root, {sid: moved})
    assert updated.sources[0].id == sid
    assert updated.sources[0].fingerprint == project.sources[0].fingerprint
    assert updated.rois == project.rois
    assert updated.overrides == project.overrides
    assert updated.settings == project.settings
    assert cache_get(root, "test", sid, "key") == {"kept": True}
    validate_sources(root, updated)
    restored = resolve_snapshot_sources(project, updated)
    assert restored.sources[0].path == moved.resolve()
    result = render(root, restored, plan, root / "renders" / "relocated.mp4", encoder="libx264")
    verify_output(result, plan.actual_frame_count, 30)
    assert hashlib.sha256(moved.read_bytes()).hexdigest() == digest
    os.utime(moved, ns=(moved.stat().st_atime_ns, moved.stat().st_mtime_ns + 10000000))
    with pytest.raises(MediaError, match="reimport"):
        validate_sources(root, updated)


def test_wrong_candidate_and_cancel_leave_project_unchanged(tmp_path: Path) -> None:
    original, other = tmp_path / "original.mp4", tmp_path / "wrong.mp4"
    video(original)
    video(other, "red")
    root = tmp_path / "project"
    project = create_project(root, [original])
    sid = project.sources[0].id
    before = (root / "project.json").read_bytes()
    with pytest.raises(ValueError, match="не совпадает"):
        relink_sources(root, {sid: other})
    assert (root / "project.json").read_bytes() == before
    cancel = Event()
    cancel.set()
    with pytest.raises(Cancelled):
        relink_sources(root, {sid: original}, cancel)
    with pytest.raises(ValueError, match="Неизвестный"):
        relink_sources(root, {"unknown": original})
    assert (root / "project.json").read_bytes() == before


def test_legacy_move_and_changed_copy(tmp_path: Path) -> None:
    original = tmp_path / "old.mp4"
    video(original)
    root = tmp_path / "project"
    project = create_project(root, [original])
    project.sources[0] = project.sources[0].model_copy(update={"fingerprint_mtime_ns": None})
    save_project(root, project)
    moved = tmp_path / "new.mp4"
    shutil.copy2(original, moved)
    original.unlink()
    sid = project.sources[0].id
    relink_sources(root, {sid: moved})
    # Older projects without the original timestamp can still accept a normal move.
    assert load_project(root).sources[0].path == moved


def test_gui_prompts_for_missing_source_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    original = tmp_path / "original.mp4"
    video(original)
    root = tmp_path / "project"
    project = create_project(root, [original])
    moved = tmp_path / "moved.mp4"
    original.rename(moved)
    window = MainWindow()
    window.show()
    calls: list[str] = []

    def choose(*args: object) -> tuple[str, str]:
        calls.append(str(args[1]))
        return "", ""

    monkeypatch.setattr(QFileDialog, "getOpenFileName", choose)
    try:
        window.open_project_folder(root)
        app.processEvents()
        assert "original.mp4" in calls[0]
        assert window.relink_button.isVisible()
        assert load_project(root).sources[0].path == original
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_: (str(moved), ""))
        window.relink_button.click()
        deadline = time.monotonic() + 20
        while window.job and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert window.job is None
        assert not window.last_error
        assert window.source_paths() == [moved.resolve()]
        assert not window.relink_button.isVisible()
        assert load_project(root).sources[0].fingerprint == project.sources[0].fingerprint
    finally:
        window.close()
        app.processEvents()
