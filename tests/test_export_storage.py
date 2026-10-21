import json
import os
from pathlib import Path

import pytest

from sculptcut.export_storage import has_exports, metadata_path, ordered_exports, organize_exports
from sculptcut.variants import cut_history, read_variant, save_variant


def test_managed_outputs_only_contain_videos(tmp_path: Path) -> None:
    folder = tmp_path / "renders"
    folder.mkdir()
    video = folder / "original.mp4"
    video.write_bytes(b"unchanged video")
    music = tmp_path / "music.wav"
    music.write_bytes(b"saved music")
    save_variant(video, music=music)
    cut = folder / "cut.mp4"
    cut.touch()
    save_variant(cut, parent=video, operation="cut", cuts=[(10, 20)])
    assert {p.suffix for p in folder.iterdir()} == {".mp4"}
    assert cut_history(cut) == [[(10, 20)]]
    assert (folder / read_variant(cut)["music"]["path"]).read_bytes() == b"saved music"


def test_legacy_migration_preserves_music_cuts_and_video(tmp_path: Path) -> None:
    folder = tmp_path / "renders"
    folder.mkdir()
    video = folder / "old.mp4"
    video.write_bytes(b"unchanged video")
    assets = folder / ".variant-assets" / video.stem
    assets.mkdir(parents=True)
    (assets / "soundtrack.wav").write_bytes(b"music")
    video.with_suffix(".variant.json").write_text(json.dumps({
        "schema_version": 1, "operation": "cut", "cuts_us": [[10, 20]],
        "cut_history_us": [[[10, 20]]],
        "music": {"path": ".variant-assets/old/soundtrack.wav"},
    }), encoding="utf-8")
    video.with_suffix(".guard.json").write_text("{}", encoding="utf-8")
    jobs = folder / ".sculptcut-jobs" / "checkpoint"
    jobs.mkdir(parents=True)
    (jobs / "manifest.json").write_text("{}", encoding="utf-8")
    organize_exports(tmp_path)
    organize_exports(tmp_path)
    assert list(folder.iterdir()) == [video]
    assert video.read_bytes() == b"unchanged video"
    assert cut_history(video) == [[(10, 20)]]
    assert (folder / read_variant(video)["music"]["path"]).read_bytes() == b"music"
    assert metadata_path(video, ".guard.json").is_file()
    assert (tmp_path / "export-data/renders/.sculptcut-jobs/checkpoint/manifest.json").is_file()


def test_migration_refuses_conflicts_and_preserves_unknown_files(tmp_path: Path) -> None:
    folder = tmp_path / "renders"
    folder.mkdir()
    video = folder / "old.mp4"
    video.touch()
    old = video.with_suffix(".variant.json")
    old.write_text("old", encoding="utf-8")
    new = metadata_path(video, ".variant.json")
    new.parent.mkdir(parents=True)
    new.write_text("different", encoding="utf-8")
    unknown = folder / "notes.json"
    unknown.write_text("personal", encoding="utf-8")
    with pytest.raises(ValueError, match="Conflicting"):
        organize_exports(tmp_path)
    assert old.read_text() == "old"
    assert new.read_text() == "different"
    assert unknown.read_text() == "personal"


def test_export_listing_reflects_creation_removal_and_latest_version(tmp_path: Path) -> None:
    folder = tmp_path / "renders"
    assert not has_exports(folder) and ordered_exports(folder) == []
    folder.mkdir()
    (folder / "directory.mp4").mkdir()
    (folder / "notes.json").touch()
    assert not has_exports(folder)
    old, new = folder / "old.mp4", folder / "new.mp4"
    old.touch()
    new.touch()
    os.utime(old, ns=(1_000_000_000, 1_000_000_000))
    os.utime(new, ns=(2_000_000_000, 2_000_000_000))
    assert has_exports(folder)
    assert ordered_exports(folder) == [new.resolve(), old.resolve()]
    new.unlink()
    assert ordered_exports(folder) == [old.resolve()]
    old.unlink()
    assert not has_exports(folder)
