"""Delete one managed export through the existing source-protected cleanup service."""

from collections.abc import Callable, Iterable
from pathlib import Path
from threading import Event

from sculptcut.project_cleanup import (
    CleanupFile,
    CleanupResult,
    cleanup_files,
    remove_cleanup_files,
)
from sculptcut.variants import read_variant


def variant_files(
    root: Path, video: Path, extra_inputs: Iterable[Path] = (),
) -> tuple[CleanupFile, ...]:
    root, video = root.resolve(), video.absolute()
    if video.parent != root / "renders":
        raise ValueError("Можно удалить только готовую версию из текущего проекта.")
    files = tuple(entry for entry in cleanup_files(root, extra_inputs) if entry.video == video)
    if not any(entry.path == video for entry in files):
        raise ValueError("Версия недоступна для удаления или используется как исходник.")
    for other in video.parent.glob("*.mp4"):
        if other == video:
            continue
        state = read_variant(other)
        parent = state.get("parent")
        if parent and (other.parent / parent).resolve() == video and (
            state.get("operation") == "cut" or "cut_history_us" not in state
        ):
            raise ValueError(f"Сначала удалите зависимую версию: {other.name}")
    return files


def delete_variant(
    root: Path, video: Path, expected: tuple[CleanupFile, ...], cancel: Event,
    progress: Callable[[float], None], extra_inputs: Iterable[Path] = (),
) -> CleanupResult:
    # Recheck after the confirmation: a new child or a replaced file must be
    # handled before removing the video or its metadata.
    inputs = tuple(extra_inputs)
    if variant_files(root, video, inputs) != expected:
        raise ValueError("Файл изменился после выбора")
    return remove_cleanup_files(root, expected, cancel, progress, inputs)
