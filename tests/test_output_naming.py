from pathlib import Path
from threading import Event

import pytest

from sculptcut.domain import ProjectSettings
from sculptcut.export_storage import (
    metadata_path,
    music_draft_path,
    publication_lock,
    publish_export,
    variant_assets,
)
from sculptcut.output_naming import (
    clean_output_name,
    next_output_path,
    next_variant_path,
    output_suffix,
)


def test_output_name_accepts_unicode_and_optional_mp4_suffix() -> None:
    assert clean_output_name("  Моя модель.mp4  ") == "Моя модель"
    assert ProjectSettings(output_name="Dragon.MP4").output_name == "Dragon"


@pytest.mark.parametrize("name", ["bad/name", "bad:name", "CON", "name."])
def test_output_name_rejects_windows_invalid_names(name: str) -> None:
    with pytest.raises(ValueError):
        clean_output_name(name)


def test_next_output_path_never_overwrites_existing_video(tmp_path: Path) -> None:
    assert next_output_path(tmp_path, "Hero") == tmp_path / "Hero.mp4"
    (tmp_path / "Hero.mp4").touch()
    assert next_output_path(tmp_path, "Hero") == tmp_path / "Hero-002.mp4"
    (tmp_path / "Hero-002.mp4").touch()
    assert next_output_path(tmp_path, "Hero") == tmp_path / "Hero-003.mp4"

    (tmp_path / "timelapse-001.mp4").touch()
    assert next_output_path(tmp_path) == tmp_path / "timelapse-002.mp4"


@pytest.mark.parametrize(
    ("preset", "suffix"),
    [("timelapse", "_timelapse"), ("shorts", "_shorts"), ("speed", "_speed")],
)
def test_finished_output_name_includes_selected_preset(
    tmp_path: Path, preset: str, suffix: str
) -> None:
    assert output_suffix(preset) == suffix
    first = next_output_path(tmp_path, "Hero", preset)
    assert first == tmp_path / f"Hero{suffix}.mp4"
    first.touch()
    assert next_output_path(tmp_path, "Hero", preset) == tmp_path / f"Hero{suffix}-002.mp4"
    assert next_output_path(tmp_path, f"Hero{suffix}", preset) == tmp_path / f"Hero{suffix}-002.mp4"


def test_unnamed_finished_output_keeps_preset_suffix(tmp_path: Path) -> None:
    assert next_output_path(tmp_path, preset="shorts") == tmp_path / "timelapse-001_shorts.mp4"


def test_derived_variant_keeps_preset_suffix(tmp_path: Path) -> None:
    first = next_variant_path(tmp_path, "music-variant", "shorts")
    assert first == tmp_path / "music-variant-001_shorts.mp4"
    first.touch()
    assert next_variant_path(tmp_path, "music-variant", "shorts") == (
        tmp_path / "music-variant-002_shorts.mp4"
    )


def test_output_naming_and_publication_preserve_existing_companions(tmp_path: Path) -> None:
    folder = tmp_path / "renders"
    folder.mkdir()
    first = folder / "Hero_timelapse.mp4"
    companions = (
        metadata_path(first, ".variant.json"),
        variant_assets(folder / "Hero_timelapse-002.mp4") / "soundtrack.flac",
        (folder / "Hero_timelapse-003.mp4").with_suffix(".variant.json"),
        metadata_path(folder / "Hero_timelapse-004.mp4", ".guard.json"),
        metadata_path(folder / "Hero_timelapse-005.mp4", ".music-edit.json"),
        music_draft_path(tmp_path, folder / "Hero_timelapse-006.mp4"),
        publication_lock(folder / "Hero_timelapse-007.mp4") / "owner",
        metadata_path(folder / "manual-music-001_timelapse.mp4", ".variant.json"),
    )
    for path in companions:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"existing export data")
    assert next_output_path(folder, "Hero", "timelapse") == folder / "Hero_timelapse-008.mp4"
    assert next_variant_path(folder, "manual-music", "timelapse") == (
        folder / "manual-music-002_timelapse.mp4"
    )
    ready = tmp_path / "ready.mp4"
    ready.write_bytes(b"new render")
    with pytest.raises(FileExistsError, match="Export path already in use"), publish_export(
        ready, first, Event(),
    ):
        pytest.fail("A collision must be rejected before writing any companion files")
    assert not first.exists() and ready.read_bytes() == b"new render"
    assert all(path.read_bytes() == b"existing export data" for path in companions)
