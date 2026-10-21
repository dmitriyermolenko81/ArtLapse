from pathlib import Path

import pytest

from sculptcut.domain import ProjectSettings
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
