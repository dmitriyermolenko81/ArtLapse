"""Keep managed export directories free of generated metadata."""

import hashlib
import logging
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Event

LOGGER = logging.getLogger(__name__)


def has_exports(directory: Path) -> bool:
    """Stop at the first video without materializing every directory entry."""
    try:
        with os.scandir(directory) as entries:
            return any(os.path.normcase(entry.name).endswith(".mp4") and entry.is_file()
                       for entry in entries)
    except (FileNotFoundError, NotADirectoryError):
        return False


def ordered_exports(directory: Path) -> list[Path]:
    """Read metadata once and resolve the directory once, rather than every video."""
    directory = directory.resolve()
    videos: list[tuple[int, str, Path]] = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not os.path.normcase(entry.name).endswith(".mp4"):
                    continue
                try:
                    if entry.is_file():
                        path = Path(entry.path)
                        videos.append((entry.stat().st_mtime_ns, entry.name,
                                       path.resolve() if entry.is_symlink() else path))
                except FileNotFoundError:
                    continue
    except (FileNotFoundError, NotADirectoryError):
        return []
    return [path for _, _, path in sorted(videos, reverse=True)]


def metadata_directory(video: Path) -> Path:
    if video.parent.name in ("renders", "previews"):
        return video.parent.parent / "export-data" / video.parent.name
    return video.parent


def metadata_path(video: Path, suffix: str) -> Path:
    return metadata_directory(video) / video.with_suffix(suffix).name


def music_relative_path(music: Path, video: Path) -> str:
    return os.path.relpath(music, video.parent)


def publication_lock(video: Path) -> Path:
    return metadata_path(video, ".publishing")


def variant_assets(video: Path) -> Path:
    return metadata_directory(video) / ".variant-assets" / video.stem


def music_draft_path(root: Path, video: Path) -> Path:
    key = hashlib.sha256(str(video.resolve()).casefold().encode()).hexdigest()[:20]
    return root / "music-edit-drafts" / f"{key}.json"


def export_path_in_use(
    video: Path, *, include_lock: bool = True, extra_files: tuple[Path, ...] = (),
) -> bool:
    """Reserve names held by existing exports or by their saved companion data."""
    if include_lock and os.path.lexists(publication_lock(video)):
        return True
    suffixes = (".variant.json", ".guard.json", ".music-edit.json")
    assets = variant_assets(video)
    paths = (
        video, assets, video.parent / ".variant-assets" / video.stem,
        *(metadata_path(video, suffix) for suffix in suffixes),
        *(video.with_suffix(suffix) for suffix in suffixes),
        *extra_files,
    )
    if any(os.path.lexists(path) for path in paths):
        return True
    return video.parent.name in ("renders", "previews") and os.path.lexists(
        music_draft_path(video.parent.parent, video))


@contextmanager
def publish_export(
    ready: Path, video: Path, cancel: Event | None,
    *, extra_files: tuple[Path, ...] = (),
) -> Iterator[None]:
    """Write companion files under a reservation, then publish the verified MP4."""
    from sculptcut.media import Cancelled

    lock = publication_lock(video)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.mkdir()
    assets = variant_assets(video)
    files = (metadata_path(video, ".variant.json"), *extra_files)
    try:
        if export_path_in_use(video, include_lock=False, extra_files=extra_files):
            raise FileExistsError(f"Export path already in use: {video.name}")
        if cancel is not None and cancel.is_set():
            raise Cancelled("Сохранение отменено")
        try:
            yield
            if cancel is not None and cancel.is_set():
                raise Cancelled("Сохранение отменено")
            if os.name == "nt":
                ready.rename(video)  # Windows rename cannot replace an existing file.
            else:
                os.link(ready, video)
        except BaseException:
            for path in (*files, assets):
                try:
                    if path == assets and path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink(missing_ok=True)
                except OSError:
                    LOGGER.exception("Could not roll back export file %s", path)
            raise
    finally:
        try:
            lock.rmdir()
        except OSError:
            LOGGER.exception("Could not remove export reservation %s", lock)


def organize_exports(root: Path) -> None:
    """Move only recognized generated files; never move or overwrite videos."""
    root = root.resolve()
    for name in ("renders", "previews"):
        directory = root / name
        if not directory.is_dir():
            continue
        target = root / "export-data" / name
        if directory.is_symlink() or target.is_symlink() or target.parent.is_symlink():
            raise ValueError("Export storage directories must not be symlinks")
        with os.scandir(directory) as scanned:
            entries = list(scanned)
        videos = {os.path.normcase(entry.name): directory / entry.name for entry in entries
                  if os.path.normcase(entry.name).endswith(".mp4")}
        for entry in entries:
            for suffix in (".variant.json", ".guard.json"):
                if not os.path.normcase(entry.name).endswith(suffix):
                    continue
                video = videos.get(os.path.normcase(entry.name[:-len(suffix)] + ".mp4"))
                if video is None:
                    continue
                old = Path(entry.path)
                new = metadata_path(video, suffix)
                if not old.is_file() or old.is_symlink():
                    continue
                target.mkdir(parents=True, exist_ok=True)
                if new.exists():
                    if new.is_symlink() or new.read_bytes() != old.read_bytes():
                        raise ValueError(f"Conflicting export metadata: {old.name}")
                    old.unlink()
                else:
                    old.rename(new)
        # These are application-owned folders, unlike arbitrary neighbouring files.
        for folder in (".variant-assets", ".sculptcut-jobs"):
            old_folder = directory / folder
            new_folder = target / folder
            if not old_folder.is_dir() or old_folder.is_symlink():
                continue
            new_folder.mkdir(parents=True, exist_ok=True)
            if new_folder.is_symlink():
                raise ValueError("Export storage directories must not be symlinks")
            for child in old_folder.iterdir():
                destination = new_folder / child.name
                if child.is_symlink() or destination.exists():
                    raise ValueError(f"Conflicting export assets: {child.name}")
                shutil.move(str(child), str(destination))
            old_folder.rmdir()
