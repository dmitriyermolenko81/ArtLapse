"""Keep managed export directories free of generated metadata."""

import os
import shutil
from pathlib import Path


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
