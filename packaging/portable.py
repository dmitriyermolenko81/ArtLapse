"""Stage a self-contained Windows folder and ZIP from a verified onedir build."""

import argparse
import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        result = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def package(source: Path, ffmpeg: Path, destination: Path, archive: Path) -> None:
    source, ffmpeg, destination, archive = (
        p.resolve() for p in (source, ffmpeg, destination, archive)
    )
    if destination.exists() or archive.exists():
        raise ValueError("Destination and archive must not exist")
    if destination.is_relative_to(source) or archive.is_relative_to(destination):
        raise ValueError("Output paths must be outside the input/staged folder")
    for path in (source / "ArtLapse.exe", source / "artlapse-cli.exe",
                 ffmpeg / "bin/ffmpeg.exe", ffmpeg / "bin/ffprobe.exe",
                 ffmpeg / "LICENSE", ffmpeg / "README.txt"):
        if not path.is_file():
            raise ValueError(f"Missing required distribution file: {path}")
    versions = {}
    for name in ("ffmpeg", "ffprobe"):
        executable = ffmpeg / "bin" / f"{name}.exe"
        version = subprocess.run(
            [str(executable), "-version"], capture_output=True, text=True, check=True,
            timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
        if "--enable-nonfree" in version:
            raise ValueError("Non-redistributable FFmpeg build")
        versions[name] = {"version": version.splitlines()[0], "sha256": digest(executable)}
    shutil.copytree(source, destination)
    tools = destination / "bin"
    tools.mkdir()
    for path in (ffmpeg / "bin").iterdir():
        if path.name in ("ffmpeg.exe", "ffprobe.exe") or path.suffix.lower() == ".dll":
            shutil.copy2(path, tools / path.name)
    licenses = destination / "licenses" / "ffmpeg"
    licenses.mkdir(parents=True)
    for name in ("LICENSE", "README.txt"):
        shutil.copy2(ffmpeg / name, licenses / name)
    (licenses / "versions.json").write_text(
        json.dumps(versions, indent=2), encoding="utf-8",
    )
    (destination / "portable.txt").write_text("ArtLapse portable v1\n", encoding="utf-8")
    shutil.copy2(Path(__file__).parent / "Start.txt", destination / "Start.txt")
    shutil.copy2(Path(__file__).parent / "НАЧНИТЕ_ЗДЕСЬ.txt", destination)
    shutil.copy2(Path(__file__).parent / "START_HERE.txt", destination)
    project_root = Path(__file__).resolve().parent.parent
    shutil.copy2(project_root / "LICENSE", destination / "LICENSE")
    shutil.copy2(Path(__file__).parent / "THIRD_PARTY_NOTICES.md", destination)
    for name in ("LIBRARY_REPLACEMENT.md", "DATA_AND_FEEDBACK.md"):
        (destination / "docs").mkdir(exist_ok=True)
        shutil.copy2(project_root / "docs" / name, destination / "docs" / name)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for path in sorted(destination.rglob("*")):
            if path.is_file():
                output.write(path, Path("ArtLapse") / path.relative_to(destination))
    archive.with_suffix(".zip.sha256").write_text(
        f"{digest(archive)}  {archive.name}\n", encoding="utf-8",
    )
    print(json.dumps({"archive": str(archive), "bytes": archive.stat().st_size,
                      "ffmpeg": versions}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--ffmpeg-root", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args()
    package(args.source, args.ffmpeg_root, args.destination, args.archive)
