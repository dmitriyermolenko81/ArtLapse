"""Local application state for a marked, unpacked portable distribution."""

import os
import sys
from pathlib import Path


def environment_value(name: str) -> str | None:
    """Prefer ArtLapse configuration while accepting existing local launch scripts."""
    return os.environ.get(f"ARTLAPSE_{name}") or os.environ.get(f"SCULPTCUT_{name}")


def portable_root() -> Path | None:
    root = Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False) and (root / "portable.txt").is_file():
        return root
    return None


def data_directory() -> Path | None:
    configured = environment_value("DATA_DIR")
    if configured:
        return Path(configured)
    root = portable_root()
    if root is not None:
        return root / "data"
    if getattr(sys, "frozen", False):
        local = Path(os.environ["LOCALAPPDATA"])
        current, legacy = local / "ArtLapse", local / "SculptCut"
        # Never move or hide existing non-portable users' preferences and diagnostics.
        return legacy if not current.exists() and legacy.is_dir() else current
    return None
