from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QFileDialog
from test_timeline import make_source

from sculptcut.domain import Project
from sculptcut.planning import build_plan
from sculptcut.project import load_project, save_project
from sculptcut.ui import MainWindow
from sculptcut.variants import read_variant, save_variant


def test_snapshot_inherits_plan_music_and_cuts(tmp_path: Path) -> None:
    project = Project(sources=[make_source(0, 0, 10_000_000)])
    plan = build_plan(project, [], 2)
    video = tmp_path / "one.mp4"
    video.touch()
    music = tmp_path / "music.flac"
    music.write_bytes(b"original audio")
    save_variant(video, project=project, plan=plan, music=music)
    music.write_bytes(b"changed cache")
    cut = tmp_path / "cut.mp4"
    cut.touch()
    save_variant(cut, parent=video, cuts=[(1_000_000, 2_000_000)], operation="cut")
    state = read_variant(cut)
    assert state["plan"] == plan.model_dump(mode="json")
    assert state["project"] == project.model_dump(mode="json")
    assert (tmp_path / state["music"]["path"]).read_bytes() == b"original audio"
    assert state["cuts_us"] == [[1_000_000, 2_000_000]]
    with pytest.raises(ValueError):
        save_variant(video, project=project)


def test_restore_selected_variant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = QApplication.instance() or QApplication([])
    project = Project(sources=[make_source(0, 0, 10_000_000)])
    project = project.model_copy(
        update={
            "settings": project.settings.model_copy(update={"target_seconds": 120, "style": "calm"})
        }
    )
    save_project(tmp_path, project)
    video = tmp_path / "renders" / "one.mp4"
    video.parent.mkdir(exist_ok=True)
    video.touch()
    save_variant(video, project=project, plan=build_plan(project, [], 2))
    window = MainWindow()
    monkeypatch.setattr(window, "open_video", lambda *a, **kw: None)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a: ("", ""))
    try:
        window.show()
        window.folder.setText(str(tmp_path))
        app.processEvents()
        assert window.variants.count() == 1
        assert window.versions_panel.isVisible()
        window.target.setValue(9)
        window.restore_variant()
        assert window.target.value() == 2
        assert load_project(tmp_path).settings.style == "calm"
        assert (tmp_path / "render_plan.json").is_file()
    finally:
        window.close()
        app.processEvents()


def test_legacy_cut_history_and_self_contained_snapshots(tmp_path: Path) -> None:
    import json

    from sculptcut.variants import cut_history

    original = tmp_path / "original.mp4"
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    save_variant(original)
    save_variant(first, parent=original, cuts=[(10, 20)], operation="cut")
    for video in (original, first):
        state = read_variant(video)
        state.pop("cut_history_us")
        video.with_suffix(".variant.json").write_text(json.dumps(state), encoding="utf-8")
    save_variant(second, parent=first, cuts=[(30, 40)], operation="cut")
    assert cut_history(second) == [[(10, 20)], [(30, 40)]]
    first.with_suffix(".variant.json").unlink()
    assert cut_history(second) == [[(10, 20)], [(30, 40)]]


def test_refilled_plan_does_not_reapply_baked_cuts(tmp_path: Path) -> None:
    from sculptcut.variants import cut_history

    video = tmp_path / "refilled.mp4"
    save_variant(video, operation="shorts_refill", cuts=[(10, 20)])
    assert cut_history(video) == []
