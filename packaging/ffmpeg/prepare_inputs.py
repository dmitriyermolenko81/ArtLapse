# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify locked inputs and extract them without overwriting existing build trees."""

import hashlib
import json
import tarfile
import zipfile
from pathlib import Path


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def main() -> None:
    script = Path(__file__).resolve().parent
    work = script.parents[1] / "artifacts/ffmpeg-own-202"
    inputs = json.loads((script / "inputs.lock.json").read_text(encoding="utf-8"))["inputs"]
    sources = work / "sources"
    tools = work / "tools"
    for entry in inputs:
        archive = work / "downloads" / entry["file"]
        if archive.stat().st_size != entry["bytes"] or digest(archive) != entry["sha256"]:
            raise ValueError(f"Input hash mismatch: {archive.name}")
        if entry.get("upstream_sha256", entry["sha256"]) != entry["sha256"]:
            raise ValueError(f"Upstream hash mismatch: {archive.name}")
        print(f"VERIFIED {archive.name}")
        if not entry.get("extract", True):
            continue  # Corresponding source for prebuilt build tools/runtime; not compiled here.
        if archive.suffix == ".whl":
            continue  # Install locally with pip --no-index --no-deps --target tools/python.
        destination = (
            tools if archive.name in ("llvm-verified.zip", "nasm-3.01-win64.zip") else sources
        )
        destination.mkdir(parents=True, exist_ok=True)
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as zipped:
                names = zipped.namelist()
                if any(
                    not (destination / n).resolve().is_relative_to(destination.resolve())
                    for n in names
                ):
                    raise ValueError("Archive path traversal")
                roots = {Path(name).parts[0] for name in names}
                if any((destination / root).exists() for root in roots):
                    print("  existing extraction preserved")
                    continue
                zipped.extractall(destination)
        else:
            with tarfile.open(archive) as tar:
                members = tar.getmembers()
                for member in members:
                    target = (destination / member.name).resolve()
                    if not target.is_relative_to(destination.resolve()):
                        raise ValueError("Archive path traversal")
                    if member.issym() or member.islnk() or member.isdev():
                        raise ValueError(f"Unsupported archive member: {member.name}")
                roots = {Path(member.name).parts[0] for member in members}
                if any((destination / root).exists() for root in roots):
                    print("  existing extraction preserved")
                    continue
                tar.extractall(destination)


if __name__ == "__main__":
    main()
