"""Share project paths between releases without moving project data or caches."""

import hashlib
import json
from pathlib import Path

from PySide6.QtCore import QSettings

from sculptcut.runtime_paths import portable_root


def project_paths(settings: QSettings) -> list[str]:
    value = settings.value("recent_projects", [])
    values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    return list(dict.fromkeys(p for p in values if isinstance(p, str) and p))


def merge_projects(shared: QSettings, previous: QSettings) -> None:
    """Import changed lists once; reopening old installs must not revive deleted entries."""
    values = project_paths(previous)
    key = "imports/" + hashlib.sha256(previous.fileName().casefold().encode()).hexdigest()
    signature = hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()
    shared.sync()
    snapshot = key + "_paths"
    previous_signature = shared.value(key)
    if previous_signature == signature and shared.contains(snapshot):
        return
    paths = project_paths(shared)
    known = {str(Path(p)).casefold() for p in paths}
    prior = shared.value(snapshot, [])
    if isinstance(prior, list):
        known.update(str(Path(p)).casefold() for p in prior if isinstance(p, str) and p)
    # Upgrade a digest-only record without replaying an unchanged import.
    for p in values if previous_signature != signature else []:
        if str(Path(p)).casefold() not in known:
            paths.append(p)
            known.add(str(Path(p)).casefold())
    shared.setValue("recent_projects", paths)
    shared.setValue(key, signature)
    shared.setValue(snapshot, values)
    shared.sync()


def store_projects(shared: QSettings, local: QSettings, paths: list[str]) -> None:
    """An explicit library edit also marks its compatibility copy as already seen."""
    shared.setValue("recent_projects", paths)
    shared.sync()
    local.setValue("recent_projects", paths)
    local.sync()
    merge_projects(shared, local)


def previous_settings() -> list[Path]:
    """Check neighbouring ArtLapse extractions only, never search whole drives."""
    root = portable_root()
    if root is None:
        return []
    found: set[Path] = set()
    for parent in (root.parent, root.parent.parent):
        try:
            folders = sorted(parent.iterdir())
        except OSError:
            continue
        for folder in folders:
            if not folder.name.casefold().startswith("artlapse"):
                continue
            for path in (folder / "data/settings.ini", folder / "ArtLapse/data/settings.ini"):
                if path.is_file():
                    found.add(path)
    return sorted(found)
