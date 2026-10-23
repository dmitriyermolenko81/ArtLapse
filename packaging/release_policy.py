"""Exclude an unused GPL-only Qt module from the LGPL deployment."""

from pathlib import PurePosixPath


def excluded_binary(name: str) -> bool:
    """Match runtime files, including Qt's implicitly collected input plugin."""
    return PurePosixPath(name.replace("\\", "/")).name.lower() in {
        "qt6virtualkeyboard.dll",
        "qtvirtualkeyboardplugin.dll",
        "qtvirtualkeyboard.pyd",
    }
