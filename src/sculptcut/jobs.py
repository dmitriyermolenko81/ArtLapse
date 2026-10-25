"""Owned render checkpoints and conservative cleanup of known derived files."""

import json
import re
from pathlib import Path

from sculptcut.export_storage import metadata_directory
from sculptcut.planning import RenderPlan
from sculptcut.project import atomic_write, cache_key, fingerprint


def render_job(destination: Path, plan: RenderPlan, encoder: str, preview: bool) -> Path:
    payload = plan.model_dump()
    payload["settings"].pop("request_key", None)
    # Execution batches differ from earlier one-decision receipts.
    key = cache_key(
        "render-job-10-sdr-color", payload, encoder, preview, str(destination.resolve())
    )
    directory = metadata_directory(destination) / ".sculptcut-jobs" / key
    if directory.parent.is_symlink() or directory.is_symlink():
        raise ValueError("Render checkpoint directories must not be symlinks")
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.json"
    expected = {"schema_version": 1, "key": key, "destination": str(destination.resolve())}
    if manifest.exists() and json.loads(manifest.read_text(encoding="utf-8")) != expected:
        raise ValueError("Render checkpoint ownership mismatch")
    atomic_write(manifest, json.dumps(expected))
    return directory


def reusable_chunk(directory: Path, name: str) -> bool:
    receipt = directory / f"{name}.json"
    output = directory / name
    if not receipt.is_file() or not output.is_file():
        return False
    try:
        expected = json.loads(receipt.read_text(encoding="utf-8"))
        return bool(
            isinstance(expected, dict) and expected.get("fingerprint") == fingerprint(output)
        )
    except (OSError, ValueError):
        return False


def record_chunk(directory: Path, name: str) -> None:
    atomic_write(
        directory / f"{name}.json", json.dumps({"fingerprint": fingerprint(directory / name)})
    )


def finish_job(directory: Path) -> None:
    """Only remove our checkpoint files; never recursively delete unknown contents."""
    if directory.parent.name != ".sculptcut-jobs" or not re.fullmatch(
        r"[0-9a-f]{64}", directory.name
    ):
        raise ValueError("Not an owned render job directory")
    marker = directory / "manifest.json"
    if (
        not marker.is_file()
        or json.loads(marker.read_text(encoding="utf-8")).get("key") != directory.name
    ):
        raise ValueError("Missing render ownership marker")
    for path in directory.iterdir():
        if path.is_symlink():
            continue
        if path.is_file() and (
            path.name
            in {
                "concat.txt",
                "combined.mp4",
                "normalized.mp4",
                "intro.mp4",
                "presentation.mp4",
                "with-music.mp4",
                "manifest.json",
                "frame-filter.txt",
                "still.png",
            }
            or re.fullmatch(r"chunk-\d{6}\.mp4(?:\.json)?", path.name)
        ):
            path.unlink()
    if not any(directory.iterdir()):
        directory.rmdir()
