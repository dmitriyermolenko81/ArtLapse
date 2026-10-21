"""Atomic project persistence, content cache and per-stage checkpoints."""

import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict
from pathlib import Path, PureWindowsPath
from threading import Event
from typing import Any
from uuid import uuid4

from sculptcut.domain import Project, SourceMedia
from sculptcut.media import Cancelled, MediaError, probe


def fingerprint(path: Path, mtime_ns: int | None = None) -> str:
    stat = path.stat()
    timestamp = stat.st_mtime_ns if mtime_ns is None else mtime_ns
    digest = hashlib.sha256(f"{stat.st_size}:{timestamp}".encode())
    with path.open("rb") as stream:
        for position in sorted(
            {0, max(0, stat.st_size // 2 - 32768), max(0, stat.st_size - 65536)}
        ):
            stream.seek(position)
            digest.update(stream.read(65536))
    return digest.hexdigest()


def atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def database(root: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(root / "analysis.sqlite")
    connection.execute("PRAGMA journal_mode=WAL")
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, 1):
        connection.close()
        raise ValueError(f"Unsupported cache schema: {version}")
    if version == 1:
        return connection
    with connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS cache "
            "(stage TEXT, source_id TEXT, key TEXT, value TEXT, "
            "PRIMARY KEY(stage,source_id))"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS checkpoints "
            "(stage TEXT, source_id TEXT, key TEXT, position INTEGER, "
            "PRIMARY KEY(stage,source_id))"
        )
        connection.execute("PRAGMA user_version=1")
    return connection


def save_project(root: Path, project: Project) -> None:
    atomic_write(root / "project.json", project.model_dump_json(indent=2))


def load_project(root: Path) -> Project:
    data = json.loads((root / "project.json").read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("Unsupported project schema; no legacy project migration is defined")
    return Project.model_validate(data)


def create_project(root: Path, paths: list[Path]) -> Project:
    if not paths:
        raise ValueError("At least one source is required")
    if root.exists() and any(root.iterdir()):
        raise ValueError("Project directory must be empty")
    sources = []
    offset = 0
    for index, path in enumerate(paths):
        info = probe(path)
        if info.duration_us is None:
            raise MediaError(f"Unknown or zero duration: {path}")
        source = SourceMedia(
            **asdict(info),
            id=f"source-{index:04d}",
            fingerprint=fingerprint(path),
            fingerprint_mtime_ns=path.stat().st_mtime_ns,
            global_start_us=offset,
        )
        sources.append(source)
        offset += source.duration_us
    project = Project(sources=sources)
    root.mkdir(parents=True, exist_ok=True)
    _initialize_project(root, project)
    return project


def new_project_path(parent: Path, name: str) -> Path:
    name = name.strip()
    if (not name or name in (".", "..") or name.endswith(".") or len(name) > 100
            or any(ord(char) < 32 or char in '<>:"/\\|?*' for char in name)
            or PureWindowsPath(name).is_reserved()):
        raise ValueError("Укажите допустимое имя папки (до 100 символов).")
    if not parent.is_dir():
        raise ValueError("Выберите существующую папку для проекта.")
    target = parent.resolve() / name
    if target.exists() or target.is_symlink():
        raise ValueError("Такая папка или файл уже существует. Измените название.")
    return target


def create_empty_project(root: Path) -> Project:
    """Create a saved project before importing media; never reuse an existing path."""
    root.mkdir()
    project = Project(sources=[])
    _initialize_project(root, project)
    return project


def _initialize_project(root: Path, project: Project) -> None:
    for name in ("proxy", "thumbnails", "previews", "renders", "temp", "logs"):
        (root / name).mkdir(exist_ok=True)
    database(root).close()
    save_project(root, project)


def replace_sources(project: Project, paths: list[Path]) -> Project:
    """Change the montage inputs without touching originals or retained caches."""
    retained = {s.path.resolve(): s for s in project.sources}
    sources = []
    position = 0
    for path in dict.fromkeys(p.resolve() for p in paths):
        source = retained.get(path)
        if source is None:
            info = probe(path)
            if info.duration_us is None:
                raise MediaError(f"Unknown or zero duration: {path}")
            source = SourceMedia(
                **asdict(info),
                id=f"source-{uuid4().hex}",
                fingerprint=fingerprint(path),
                fingerprint_mtime_ns=path.stat().st_mtime_ns,
                global_start_us=position,
            )
        sources.append(source.model_copy(update={"global_start_us": position}))
        position += source.duration_us
    ids = {s.id for s in sources}
    return Project.model_validate(
        project.model_dump()
        | {
            "sources": sources,
            "rois": {sid: roi for sid, roi in project.rois.items() if sid in ids},
            "overrides": [o for o in project.overrides if o.source_id in ids],
        }
    )


def validate_sources(root: Path, project: Project) -> None:
    """Invalidate derived entries for changed sources and require explicit reimport."""
    bad = [
        source
        for source in project.sources
        if not source.path.is_file()
        or fingerprint(source.path) != (source.location_fingerprint or source.fingerprint)
    ]
    if bad:
        connection = database(root)
        try:
            with connection:
                for source in bad:
                    connection.execute("DELETE FROM cache WHERE source_id=?", (source.id,))
                    connection.execute("DELETE FROM checkpoints WHERE source_id=?", (source.id,))
        finally:
            connection.close()
        raise MediaError(
            "Source changed/missing; reimport required: " + ", ".join(s.id for s in bad)
        )


def relink_sources(
    root: Path, replacements: dict[str, Path], cancel: Event | None = None,
) -> Project:
    """Verify every candidate before atomically updating paths, preserving edit identity."""
    project = load_project(root)
    if not replacements.keys() <= {s.id for s in project.sources}:
        raise ValueError("Неизвестный исходник проекта")
    sources = []
    for source in project.sources:
        if cancel is not None and cancel.is_set():
            raise Cancelled("Cancelled")
        if source.id not in replacements:
            sources.append(source)
            continue
        path = replacements[source.id].resolve()
        if not path.is_file():
            raise ValueError(f"Видео не найдено: {path}")
        before = fingerprint(path)
        if fingerprint(path, source.fingerprint_mtime_ns) != source.fingerprint:
            raise ValueError(
                f"Выбранное видео не совпадает с сохранённым исходником: {source.path.name}. "
                "Выберите прежний файл, а не другую запись или перекодированную копию. "
                "Для старого проекта копия должна сохранять дату изменения файла."
            )
        info = asdict(probe(path))
        if any(info[key] != getattr(source, key) for key in info if key != "path"):
            raise ValueError(f"Параметры видео отличаются от исходника: {source.path.name}")
        if before != fingerprint(path):
            raise ValueError("Видео изменилось во время проверки. Повторите поиск.")
        sources.append(source.model_copy(update={
            "path": path, "location_fingerprint": before,
            "fingerprint_mtime_ns": (source.fingerprint_mtime_ns
                                     if source.fingerprint_mtime_ns is not None
                                     else path.stat().st_mtime_ns),
        }))
    if len({s.path.resolve() for s in sources}) != len(sources):
        raise ValueError("Один файл нельзя назначить нескольким исходникам")
    updated = project.model_copy(update={"sources": sources})
    if cancel is not None and cancel.is_set():
        raise Cancelled("Cancelled")
    save_project(root, updated)
    return updated


def resolve_snapshot_sources(snapshot: Project, current: Project) -> Project:
    """Use verified current locations in historical edits without rewriting history."""
    known = {(s.id, s.fingerprint): s for s in current.sources}
    sources = []
    for source in snapshot.sources:
        relocated = known.get((source.id, source.fingerprint))
        if relocated is not None:
            source = source.model_copy(update={
                "path": relocated.path,
                "location_fingerprint": relocated.location_fingerprint,
                "fingerprint_mtime_ns": relocated.fingerprint_mtime_ns,
            })
        sources.append(source)
    return snapshot.model_copy(update={"sources": sources})


def cache_key(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


class AnalysisCache:
    """One operation's connection; each completed result is committed separately."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def get(self, stage: str, source_id: str, key: str) -> Any:
        row = self.connection.execute(
            "SELECT value FROM cache WHERE stage=? AND source_id=? AND key=?",
            (stage, source_id, key),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, stage: str, source_id: str, key: str, value: object) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO cache VALUES(?,?,?,?)",
                (stage, source_id, key, json.dumps(value)),
            )


@contextmanager
def cache_session(root: Path) -> Iterator[AnalysisCache]:
    """Reuse SQLite within one operation/thread and always close it on exit."""
    with closing(database(root)) as connection:
        yield AnalysisCache(connection)


def cache_get(root: Path, stage: str, source_id: str, key: str) -> Any:
    with cache_session(root) as cache:
        return cache.get(stage, source_id, key)


def cache_put(root: Path, stage: str, source_id: str, key: str, value: object) -> None:
    with cache_session(root) as cache:
        cache.put(stage, source_id, key, value)
