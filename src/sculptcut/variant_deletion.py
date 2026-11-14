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


def _dependencies(root: Path, video: Path) -> set[Path]:
    state = read_variant(video)
    parent = state.get('parent')
    if not parent or not (state.get('operation') == 'cut' or 'cut_history_us' not in state):
        return set()
    parents = {(video.parent/parent).resolve()}
    # Legacy histories store only a basename, even between previews and renders.
    # Protect both possible parents rather than guess which copy is still needed.
    if Path(parent).name == parent:
        parents.update((root/folder/parent).resolve() for folder in ('renders', 'previews'))
    return parents-{video}


def variant_files(
    root: Path, video: Path, extra_inputs: Iterable[Path] = (),
    *, deleting: Iterable[Path] = (),
) -> tuple[CleanupFile, ...]:
    root, video = root.resolve(), video.absolute()
    if video.parent not in (root / "renders", root / "previews"):
        raise ValueError("Можно удалить только готовую версию из текущего проекта.")
    files = tuple(entry for entry in cleanup_files(root, extra_inputs) if entry.video == video)
    if not any(entry.path == video for entry in files):
        raise ValueError("Версия недоступна для удаления или используется как исходник.")
    batch = set(deleting)
    for other in (p for folder in ("renders", "previews")
                  for p in (root/folder).glob("*.mp4")):
        if other == video or other in batch:
            continue
        if video in _dependencies(root, other):
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


VariantBatch = tuple[tuple[Path, tuple[CleanupFile, ...]], ...]


def batch_variant_files(root: Path, videos: Iterable[Path],
                        extra_inputs: Iterable[Path] = ()) -> VariantBatch:
    root = root.resolve()
    remaining = set(video.absolute() for video in videos)
    inputs = tuple(extra_inputs)
    files = {video: variant_files(root, video, inputs, deleting=remaining) for video in remaining}
    parents = {video: _dependencies(root, video) for video in remaining}
    ordered = []
    while remaining:
        needed = {parent for video in remaining for parent in parents[video]}
        leaves = sorted(remaining-needed)
        if not leaves:
            raise ValueError('В истории выбранных версий обнаружен цикл.')
        for video in leaves:
            ordered.append((video, files[video]))
            remaining.remove(video)
    return tuple(ordered)


def delete_variants(root: Path, expected: VariantBatch, cancel: Event,
                    progress: Callable[[float], None],
                    extra_inputs: Iterable[Path] = ()) -> CleanupResult:
    inputs = tuple(extra_inputs)
    if batch_variant_files(root, (video for video, _ in expected), inputs) != expected:
        raise ValueError('Файл изменился после выбора')
    removed = total = 0
    errors: list[str] = []
    for i, (video, files) in enumerate(expected):
        if cancel.is_set():
            break
        def advance(value: float, offset: int = i) -> None:
            progress((offset+value)/len(expected))
        result = delete_variant(root, video, files, cancel, advance, inputs)
        removed += result.removed
        total += result.bytes_removed
        errors.extend(result.errors)
        if result.errors or result.cancelled:
            break  # Keep parents if a selected dependent could not be removed.
    return CleanupResult(removed, total, tuple(errors), cancel.is_set())
