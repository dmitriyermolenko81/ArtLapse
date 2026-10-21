"""Local application state for a marked, unpacked portable distribution."""

import os
import sys
from pathlib import Path


def portable_root() -> Path | None:
    root = Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False) and (root / "portable.txt").is_file():
        return root
    return None


def data_directory() -> Path | None:
    configured = os.environ.get("SCULPTCUT_DATA_DIR")
    if configured:
        return Path(configured)
    root = portable_root()
    if root is not None:
        return root / "data"
    if getattr(sys, "frozen", False):
        return Path(os.environ["LOCALAPPDATA"]) / "SculptCut"
    return None
