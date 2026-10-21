"""Safe user-facing names for new rendered MP4 files."""

import re
from pathlib import Path

INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
PRESET_SUFFIXES = {
    "timelapse": "timelapse",
    "shorts": "shorts",
    "beat_shorts": "beat_shorts",
    "music_shorts": "music_shorts",
    "photoshop_shorts": "photoshop_shorts",
    "photoshop_beat_shorts": "photoshop_beat_shorts",
    "speed": "speed",
}


def clean_output_name(value: str) -> str:
    """Return a filename stem, accepting an optional .mp4 suffix."""
    name = value.strip()
    if name.lower().endswith(".mp4"):
        name = name[:-4].rstrip()
    if not name:
        return ""
    if len(name) > 120:
        raise ValueError("Имя ролика должно быть не длиннее 120 символов.")
    if INVALID_NAME.search(name) or name.endswith((".", " ")):
        raise ValueError(
            'Имя ролика не должно содержать < > : " / \\ | ? * '
            "или заканчиваться точкой."
        )
    if name.upper() in RESERVED_NAMES:
        raise ValueError("Это имя зарезервировано Windows. Выберите другое имя ролика.")
    return name


def output_suffix(preset: str) -> str:
    """Return the visible suffix used for a finished GUI export."""
    try:
        return "_" + PRESET_SUFFIXES[preset]
    except KeyError as exc:
        raise ValueError("Unknown output preset") from exc


def next_variant_path(folder: Path, variant: str, preset: str) -> Path:
    """Choose a numbered derivative name that retains its preset label."""
    suffix = output_suffix(preset)
    index = 1
    while (folder / f"{variant}-{index:03d}{suffix}.mp4").exists():
        index += 1
    return folder / f"{variant}-{index:03d}{suffix}.mp4"


def next_output_path(folder: Path, requested_name: str = "", preset: str = "") -> Path:
    """Choose a new MP4 path without overwriting an existing render."""
    name = clean_output_name(requested_name)
    suffix = output_suffix(preset) if preset else ""
    if not name:
        index = 1
        while (folder / f"timelapse-{index:03d}{suffix}.mp4").exists():
            index += 1
        return folder / f"timelapse-{index:03d}{suffix}.mp4"
    if suffix and not name.lower().endswith(suffix):
        name += suffix
    first = folder / f"{name}.mp4"
    if not first.exists():
        return first
    index = 2
    while (folder / f"{name}-{index:03d}.mp4").exists():
        index += 1
    return folder / f"{name}-{index:03d}.mp4"
