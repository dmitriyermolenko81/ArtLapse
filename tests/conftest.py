"""Keep GUI regression runs from writing the user's persistent recent-project list."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings


@pytest.fixture(autouse=True)
def isolated_recent_projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def settings(*args: object) -> QSettings:
        return QSettings(str(tmp_path / "recent-projects.ini"), QSettings.Format.IniFormat)

    monkeypatch.setattr("sculptcut.ui.QSettings", settings)
    yield
