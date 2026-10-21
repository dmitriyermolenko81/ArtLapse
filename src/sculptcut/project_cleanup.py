"""Explicit file-level cleanup of managed videos; never recurse or follow links."""

import os
import re
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Literal

from sculptcut.domain import Project
from sculptcut.export_storage import metadata_path
from sculptcut.project import load_project

Category = Literal["renders", "previews", "proxy"]


@dataclass(frozen=True)
class CleanupFile:
    path: Path
    video: Path
    category: Category
    size: int
    identity: tuple[int, int, int, int]


@dataclass(frozen=True)
class CleanupResult:
    removed: int
    bytes_removed: int
    errors: tuple[str, ...]
    cancelled: bool


def file_identity(path: Path) -> tuple[int, int, int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def plain_file(root: Path, path: Path) -> bool:
    if not path.is_relative_to(root) or path == root:
        return False
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            return False
        if part == root:
            break
    return path.is_file() and path.resolve().is_relative_to(root)


def protected_paths(project: Project, extra: Iterable[Path]) -> list[Path]:
    return [p.resolve() for p in (
        *(s.path for s in project.sources), project.settings.music_path,
        project.settings.music_folder, project.settings.intro_image,
        project.settings.presentation_video, *extra,
    ) if p is not None]


def protected(path: Path, inputs: list[Path]) -> bool:
    for source in inputs:
        if path == source or path.is_relative_to(source):
            return True
        if source.is_file() and os.path.samefile(path, source):
            return True
    return False


def cleanup_files(root: Path, extra_inputs: Iterable[Path] = ()) -> tuple[CleanupFile, ...]:
    """Snapshot only MP4s in managed folders and their exact known sidecars."""
    root = root.resolve()
    project = load_project(root)
    inputs = protected_paths(project, extra_inputs)
    files: list[CleanupFile] = []
    categories: tuple[Category, ...] = ("renders", "previews", "proxy")
    for kind in categories:
        directory = root / kind
        if not directory.is_dir():
            continue
        for video in sorted(directory.glob("*.mp4")):
            if kind == "proxy" and not any(
                re.fullmatch(re.escape(s.id) + r"-[0-9a-f]{16}\.mp4", video.name)
                for s in project.sources
            ):
                continue
            if not plain_file(root, video) or protected(video, inputs):
                continue
            candidates = [video]
            if kind != "proxy":
                for suffix in (".variant.json", ".guard.json"):
                    candidates.extend((metadata_path(video, suffix), video.with_suffix(suffix)))
            for path in dict.fromkeys(candidates):
                if path.exists() and plain_file(root, path) and not protected(path, inputs):
                    identity = file_identity(path)
                    files.append(CleanupFile(path, video, kind, identity[2], identity))
    return tuple(files)


def remove_cleanup_files(
    root: Path, files: tuple[CleanupFile, ...], cancel: Event,
    progress: Callable[[float], None], extra_inputs: Iterable[Path] = (),
) -> CleanupResult:
    """Recheck the allowlist and input protection before every individual unlink."""
    root = root.resolve()
    allowed = {f.path: f for f in cleanup_files(root, extra_inputs)}
    errors: list[str] = []
    removed = total = 0
    for index, entry in enumerate(files):
        # Finish the current video's sidecars before honouring cancellation.
        if cancel.is_set() and (entry.path == entry.video or entry.video.exists()):
            break
        try:
            if entry.path != entry.video and entry.video.exists():
                raise ValueError("Ролик не удалён; его история сохранена")
            if allowed.get(entry.path) != entry or not plain_file(root, entry.path):
                raise ValueError("Файл изменился или больше не относится к очистке")
            if file_identity(entry.path) != entry.identity:
                raise ValueError("Файл изменился после выбора")
            entry.path.unlink()
            removed += 1
            total += entry.size
        except (OSError, ValueError) as exc:
            errors.append(f"{entry.path.name}: {exc}")
        progress((index + 1) / len(files))
    return CleanupResult(removed, total, tuple(errors), cancel.is_set())


def format_bytes(size: int) -> str:
    from sculptcut.i18n import tr

    value = float(size)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            return f"{value:.1f} {tr(unit)}" if unit != "Б" else tr(f"{size} Б")
        value /= 1024
    raise AssertionError("unreachable")
