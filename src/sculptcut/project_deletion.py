"""Delete a confirmed project folder without following links or touching source media."""

import stat
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from sculptcut.project import load_project
from sculptcut.project_cleanup import (
    CleanupResult,
    file_identity,
    plain_file,
    protected,
    protected_paths,
)


@dataclass(frozen=True)
class ProjectFolder:
    root: Path
    identity: tuple[int, int]
    project_identity: tuple[int, int, int, int]


def directory_identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400):
        raise ValueError("Удаление папок со ссылками или перенаправлениями недоступно.")
    return info.st_dev, info.st_ino


def project_deletion_target(root: Path, extra_inputs: Iterable[Path] = ()) -> ProjectFolder:
    root = root.absolute()
    # Inspect the lexical path before resolving: a junction must never turn into a target.
    for parent in (root, *root.parents):
        directory_identity(parent)
    root = root.resolve()
    if root.parent == root or any(
        place.resolve().is_relative_to(root)
        for place in (Path.home(), Path.cwd(), Path(sys.executable).parent)
    ) or any((root / name).exists() for name in (".git", "pyproject.toml", "SculptCut.exe")):
        raise ValueError("Эту папку нельзя удалить как проект. Выберите отдельную папку SculptCut.")
    marker = root / "project.json"
    if not plain_file(root, marker):
        raise ValueError("Папка не содержит обычный файл проекта SculptCut.")
    project = load_project(root)
    for path in protected_paths(project, extra_inputs):
        if path == root or path.is_relative_to(root):
            raise ValueError(
                "Внутри проекта находятся исходники, музыка или заставка. "
                "Сначала перенесите их и обновите пути в проекте."
            )
    return ProjectFolder(root, directory_identity(root), file_identity(marker))


def check_folder(target: ProjectFolder, path: Path) -> None:
    if not path.is_relative_to(target.root):
        raise ValueError("Путь находится вне подтверждённой папки проекта.")
    for parent in (path, *path.parents):
        directory_identity(parent)
    if directory_identity(target.root) != target.identity:
        raise ValueError("Папка проекта изменилась после подтверждения.")


def remove_project_folder(
    target: ProjectFolder, cancel: Event, progress: Callable[[float], None],
    extra_inputs: Iterable[Path] = (),
) -> CleanupResult:
    """Validate the complete tree first, then unlink only the inspected files; marker last."""
    root = target.root
    removed = total = 0
    marker = root / "project.json"
    inputs = tuple(extra_inputs)
    try:
        if project_deletion_target(root, inputs) != target:
            raise ValueError("Папка проекта изменилась после подтверждения. "
                             "Откройте удаление заново.")
        protected_inputs = protected_paths(load_project(root), inputs)
        files: list[tuple[Path, tuple[int, int, int, int]]] = []
        directories: list[tuple[Path, tuple[int, int]]] = []
        pending = [root]
        while pending:
            if cancel.is_set():
                return CleanupResult(0, 0, (), True)
            parent = pending.pop()
            check_folder(target, parent)
            directories.append((parent, directory_identity(parent)))
            for path in sorted(parent.iterdir()):
                if cancel.is_set():
                    return CleanupResult(0, 0, (), True)
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError("В проекте есть ссылки или перенаправления. "
                                     "Удаление отменено.")
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode) and plain_file(root, path):
                    if protected(path, protected_inputs):
                        raise ValueError("В проекте есть файл исходника или его жёсткая ссылка.")
                    files.append((path, file_identity(path)))
                else:
                    raise ValueError("В папке есть неподдерживаемый тип файла. Удаление отменено.")
            progress(-1)
        if project_deletion_target(root, inputs) != target:
            raise ValueError("Проект изменился во время проверки. Удаление отменено.")
        marker_data = marker.read_bytes()
        ordinary = [(path, identity) for path, identity in files if path != marker]
        # Validate the entire snapshot before the first destructive operation.
        for path, identity in files:
            if not plain_file(root, path) or file_identity(path) != identity:
                raise ValueError("Файлы проекта изменились во время проверки. Удаление отменено.")
        for index, (path, identity) in enumerate(ordinary):
            if cancel.is_set():
                return CleanupResult(removed, total, (), True)
            check_folder(target, path.parent)
            if (not plain_file(root, path)
                    or file_identity(path) != identity):
                raise ValueError("Папка или файл изменились. Дальнейшее удаление остановлено.")
            path.unlink()
            removed += 1
            total += identity[2]
            progress((index + 1) / (len(ordinary) + 1))
        for path, folder_identity in reversed(directories):
            if cancel.is_set():
                return CleanupResult(removed, total, (), True)
            check_folder(target, path)
            if directory_identity(path) != folder_identity:
                raise ValueError("Папка изменилась. Дальнейшее удаление остановлено.")
            if path != root:
                # rmdir refuses new files; it never recursively follows a changed directory.
                path.rmdir()
        if (set(root.iterdir()) != {marker} or not plain_file(root, marker)
                or file_identity(marker) != target.project_identity):
            raise ValueError("В проекте появились новые файлы. Удаление остановлено.")
        if cancel.is_set():
            return CleanupResult(removed, total, (), True)
        marker.unlink()
        try:
            root.rmdir()
        except OSError:
            # Preserve the project descriptor on partial failure without overwriting a new one.
            if directory_identity(root) == target.identity and not marker.exists():
                with marker.open("xb") as stream:
                    stream.write(marker_data)
            raise
        progress(1)
        return CleanupResult(removed + 1, total + len(marker_data), (), False)
    except (OSError, ValueError) as exc:
        return CleanupResult(removed, total, (str(exc),), cancel.is_set())
