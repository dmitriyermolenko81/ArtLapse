"""Packaging must omit the unused GPL-only input module without hiding other Qt DLLs."""

import runpy
from pathlib import Path

import pytest

policy = runpy.run_path(str(Path(__file__).resolve().parents[1] / "packaging/release_policy.py"))


@pytest.mark.parametrize("name", [
    "PySide6/Qt6VirtualKeyboard.dll",
    r"PySide6\plugins\platforminputcontexts\qtvirtualkeyboardplugin.dll",
    "PySide6/QtVirtualKeyboard.pyd",
])
def test_exclude_virtual_keyboard(name: str) -> None:
    assert policy["excluded_binary"](name)


@pytest.mark.parametrize("name", [
    "PySide6/Qt6Gui.dll", "PySide6/Qt6Widgets.dll",
    "PySide6/plugins/platforms/qwindows.dll", "PySide6/Qt6Multimedia.dll",
    "PySide6/Qt6Pdf.dll", "PySide6/avcodec-61.dll",
])
def test_keep_required_and_other_components(name: str) -> None:
    assert not policy["excluded_binary"](name)
