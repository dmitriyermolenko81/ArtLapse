"""Bounded project-local storage of verified original-quality render intermediates."""

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from sculptcut.project import fingerprint

CACHE_BYTES = 8 * 1024**3


def _safe_folder(folder: Path) -> bool:
    return not (folder.is_symlink() or folder.parent.is_symlink())


def restore(folder: Path, key: str, target: Path, *, private: bool = False) -> list[int] | None:
    entry = folder / key
    try:
        if not _safe_folder(folder) or entry.is_symlink():
            return None
        stamp, guard = json.loads((entry / "receipt.json").read_text(encoding="utf-8"))
        media = entry / "video"
        if stamp != fingerprint(media) or not isinstance(guard, list):
            return None
        target.unlink(missing_ok=True)
        if private:
            os.link(media, target)
        else:
            shutil.copy2(media, target)
        return guard
    except (OSError, ValueError, TypeError):
        return None


def remember(folder: Path, key: str, source: Path, guard: list[int], *, private: bool = False
             ) -> None:
    entry = folder / key
    try:
        if not _safe_folder(folder) or entry.exists() or source.stat().st_size > CACHE_BYTES:
            return
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as scratch:
            staged = Path(scratch) / "entry"
            staged.mkdir()
            media = staged / "video"
            if private:
                try:
                    os.link(source, media)
                except OSError:
                    shutil.copy2(source, media)
            else:
                shutil.copy2(source, media)
            (staged / "receipt.json").write_text(
                json.dumps([fingerprint(media), guard]), encoding="utf-8")
            staged.rename(entry)
    except OSError:
        # Cache space and concurrent writers must not prevent delivery.
        return


def trim_cache(folder: Path) -> None:
    if not _safe_folder(folder) or not folder.is_dir():
        return
    entries = []
    for entry in folder.iterdir():
        if not re.fullmatch(r"[0-9a-f]{64}", entry.name) or entry.is_symlink():
            continue
        try:
            stat = (entry / "video").stat()
            entries.append((entry.stat().st_mtime_ns, stat.st_size, entry))
        except OSError:
            continue
    used = 0
    for _, size, entry in sorted(entries, reverse=True):
        used += size
        if used > CACHE_BYTES:
            try:
                (entry / "receipt.json").unlink(missing_ok=True)
                (entry / "video").unlink(missing_ok=True)
                entry.rmdir()
            except OSError:
                continue
