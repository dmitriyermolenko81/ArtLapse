# SPDX-License-Identifier: GPL-3.0-or-later
"""Stage a separate candidate and assemble its exact local source/license kit."""

import json
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

from prepare_inputs import digest

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "artifacts/ffmpeg-own-202"
BASE = ROOT / "dist/ArtLapse-201-TransparentLogo"
CANDIDATE = ROOT / "dist/ArtLapse-202-OwnFFmpeg"
BACKUP = ROOT / "artifacts/restore-before-ffmpeg-2026-10-22"
KIT = WORK / "source-kit"


def preserve() -> dict[str, object]:
    manifest = json.loads((BACKUP / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        for folder in (BASE, BACKUP / "ArtLapse"):
            assert digest(folder / entry["path"]) == entry["sha256"], entry["path"]
    bundle = BACKUP / "ArtLapse-source.bundle"
    assert digest(bundle) == manifest["source_bundle_sha256"]
    friend = ROOT / "dist/SculptCut-199-TaskbarIcon-Windows-x64.zip"
    assert digest(friend) == manifest["previous_friend_zip_sha256"]
    commit = subprocess.check_output(
        ["git", "rev-parse", "restore-artlapse-before-ffmpeg-2026-10-22^{commit}"],
        cwd=ROOT,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    ).strip()
    assert commit == "290a2c2d63b751fed15df187b5ec249db5d2c19f"
    result = {
        "status": "PASS",
        "base_and_backup_files_each": len(manifest["files"]),
        "bundle_sha256": digest(bundle),
        "friend_zip_sha256": digest(friend),
        "tag_commit": commit,
        "restore_manifest_sha256": digest(BACKUP / "manifest.json"),
    }
    (WORK / "preservation.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def licenses() -> None:
    destination = KIT / "licenses"
    inputs = json.loads((ROOT / "packaging/ffmpeg/inputs.lock.json").read_text(encoding="utf-8"))
    for entry in inputs["inputs"]:
        archive = WORK / "downloads" / entry["file"]
        assert digest(archive) == entry["sha256"]
        if ".tar." not in archive.name:
            continue
        with tarfile.open(archive, "r|*") as tar:
            for member in tar:
                if not member.isfile() or not re.match(
                    r"^(COPYING|COPYRIGHT|LICENSE|LICENCE|NOTICE|PATENTS|AUTHORS|CREDITS|LICENSING)",
                    Path(member.name).name,
                    re.I,
                ):
                    continue
                if (
                    archive.name.startswith("llvm-project-")
                    and not any(
                        f"/{name}/" in member.name
                        for name in (
                            "libcxx",
                            "libcxxabi",
                            "libunwind",
                            "compiler-rt",
                            "cmake",
                            "runtimes",
                        )
                    )
                    and len(Path(member.name).parts) > 2
                ):
                    continue
                target = destination / archive.name / member.name
                if not target.resolve().is_relative_to(destination.resolve()):
                    raise ValueError("Invalid license archive path")
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = tar.extractfile(member)
                assert stream is not None
                with stream, target.open("wb") as output:
                    shutil.copyfileobj(stream, output)
    toolchain = WORK / "tools/llvm-mingw-20261020-ucrt-x86_64"
    for file in toolchain.rglob("*COPYING*"):
        if file.is_file():
            target = destination / "llvm-mingw-bundled" / file.relative_to(toolchain)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, target)
    shutil.copy2(toolchain / "LICENSE.TXT", destination / "LLVM-MinGW-LICENSE.TXT")
    # CRT archive contains mixed public-domain and permissive per-file notices.
    import zipfile

    with zipfile.ZipFile(WORK / "downloads/mingw-w64-source.zip") as zipped:
        for name in zipped.namelist():
            if name.endswith("/"):
                target = destination / "mingw-w64" / name
                if target.is_file() and target.stat().st_size == 0:
                    target.unlink()
                continue
            if Path(name).name.upper().startswith(("COPYING", "LICENSE", "DISCLAIMER")):
                target = destination / "mingw-w64" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zipped.read(name))


def stage() -> None:
    preserve()
    KIT.mkdir(exist_ok=True)
    licenses()

    def ignore(directory: str, names: list[str]) -> list[str]:
        path = Path(directory)
        if path == BASE:
            return ["data"]
        if path == BASE / "licenses":
            return ["ffmpeg"]
        return []

    shutil.copytree(BASE, CANDIDATE, ignore=ignore)
    (CANDIDATE / "data").mkdir()
    shutil.copy2(BASE / "data/settings.ini", CANDIDATE / "data/settings.ini")
    binaries = {}
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        shutil.copy2(WORK / "installed/bin" / name, CANDIDATE / "bin" / name)
        binaries[name] = {
            "sha256": digest(CANDIDATE / "bin" / name),
            "bytes": (CANDIDATE / "bin" / name).stat().st_size,
        }
    shutil.copytree(KIT / "licenses", CANDIDATE / "licenses/ffmpeg-own")
    notices = (ROOT / "packaging/THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    start = notices.index("Переносной ZIP включает отдельные")
    end = notices.index("Системные runtime", start)
    notices = (
        notices[:start]
        + (
            "Этот локальный кандидат включает собственные ffmpeg.exe/ffprobe.exe "
            "8.1.2-ArtLapse-202, GPLv3+, в bin. Они собраны из зафиксированных исходников "
            "с libx264/libvpx/dav1d/Opus/zlib/nv-codec-headers. Лицензии и copyright: "
            "licenses/ffmpeg-own. Полный соответствующий исходный комплект с патчами, "
            "конфигурацией и инструкциями: sources/ArtLapse-FFmpeg-202-source.tar. "
            "Связь с бинарниками подтверждается manifest.json и SHA256. "
            "CLI запускаются отдельными процессами. Qt Multimedia содержит отдельные "
            "прежние FFmpeg DLL; их аудит194 этим не закрывается. ZIP приложения "
            "не создавался, публичная готовность не утверждается.\n\n"
        )
        + notices[end:]
    )
    (CANDIDATE / "THIRD_PARTY_NOTICES.md").write_text(notices, encoding="utf-8")
    for name in ("НАЧНИТЕ_ЗДЕСЬ.txt", "START_HERE.txt"):
        path = CANDIDATE / name
        text = path.read_text(encoding="utf-8").replace("licenses/ffmpeg", "licenses/ffmpeg-own")
        path.write_text(text, encoding="utf-8")
    (WORK / "binaries.json").write_text(json.dumps(binaries, indent=2), encoding="utf-8")
    shutil.copy2(ROOT / "docs/launch/FFMPEG_OWN_BUILD.md", CANDIDATE / "FFMPEG_OWN_BUILD.md")
    print(f"STAGED {CANDIDATE}", flush=True)


def kit() -> None:
    preserve()
    inputs = json.loads((ROOT / "packaging/ffmpeg/inputs.lock.json").read_text(encoding="utf-8"))
    (KIT / "archives").mkdir(exist_ok=True)
    for entry in inputs["inputs"]:
        archive = WORK / "downloads" / entry["file"]
        assert digest(archive) == entry["sha256"]
        if archive.name == "llvm-verified.zip":
            continue  # General-purpose compiler: pinned URL/hash; original kept in downloads.
        shutil.copy2(archive, KIT / "archives" / archive.name)
    provenance = KIT / "provenance"
    provenance.mkdir(exist_ok=True)
    for name in (
        "ffmpeg-8.1.2.tar.xz.asc",
        "ffmpeg-key.asc",
        "llvm-assets.html",
        "llvm-project-assets.html",
        "zlib-home.html",
        "meson-1.9.1-pypi.json",
        "ninja-1.13.0-pypi.json",
        "nasm-3.01-index.html",
        "nasm-win64-index.html",
    ):
        shutil.copy2(WORK / "downloads" / name, provenance / name)
    shutil.copytree(
        ROOT / "packaging/ffmpeg",
        KIT / "packaging/ffmpeg",
        ignore=shutil.ignore_patterns("__pycache__"),
        dirs_exist_ok=True,
    )
    shutil.copy2(ROOT / "docs/launch/FFMPEG_OWN_BUILD.md", KIT / "README.md")
    shutil.copytree(WORK / "logs", KIT / "logs", dirs_exist_ok=True)
    config = KIT / "configuration"
    config.mkdir(exist_ok=True)
    ffmpeg = WORK / "sources/ffmpeg-8.1.2"
    for name in (
        "config.h",
        "config_components.h",
        "config.asm",
        "ffbuild/config.mak",
        "ffbuild/config.log",
        "libavutil/ffversion.h",
    ):
        target = config / "ffmpeg" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ffmpeg / name, target)
    for source in (WORK / "sources").iterdir():
        if source.name not in {"x264-0480cb05fa188d37ae87e8f4fd8f1aea3711f7ee", "libvpx-1.15.2"}:
            continue
        for name in (
            "config.h",
            "config.mak",
            "config.log",
            "config.mk",
            "x264_config.h",
            "vpx_config.h",
            "vpx_config.asm",
            "libs-x86_64-win64-gcc.mk",
        ):
            if (source / name).exists():
                target = config / source.name / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / name, target)
    for name in ("pkgconf", "opus-1.6.1", "dav1d"):
        target = config / name
        shutil.copytree(
            WORK / "build" / name / "meson-info", target / "meson-info", dirs_exist_ok=True
        )
        shutil.copy2(WORK / "build" / name / "meson-logs/meson-log.txt", target / "meson-log.txt")
    for file in WORK.glob("*.json"):
        if file.name == "source-kit-verification.json":
            continue  # An archive cannot contain its own final digest report.
        shutil.copy2(file, KIT / file.name)
    tests = WORK / "pytest/results.xml"
    if tests.exists():
        shutil.copy2(tests, KIT / "pytest-results.xml")
    files = [
        {"path": p.relative_to(KIT).as_posix(), "bytes": p.stat().st_size, "sha256": digest(p)}
        for p in sorted(KIT.rglob("*"))
        if p.is_file() and p.name != "manifest.json"
    ]
    manifest = {
        "schema": 1,
        "component": "FFmpeg CLI only",
        "license": "GPL-3.0-or-later",
        "binaries": json.loads((WORK / "binaries.json").read_text(encoding="utf-8")),
        "files": files,
        "bit_identical_rebuild": "NOT VERIFIED",
        "whole_application_release_audit": "PARTIAL: Qt/PySide/Qt FFmpeg DLL separate",
    }
    (KIT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    archive = WORK / "ArtLapse-FFmpeg-202-source.tar"
    with tarfile.open(archive, "w") as tar:
        tar.add(KIT, arcname="ArtLapse-FFmpeg-202-source")
    destination = CANDIDATE / "sources"
    destination.mkdir(exist_ok=True)
    shutil.copy2(archive, destination / archive.name)
    sha = digest(archive)
    assert digest(destination / archive.name) == sha
    (destination / f"{archive.name}.sha256").write_text(
        f"{sha}  {archive.name}\n", encoding="utf-8"
    )
    shutil.copy2(KIT / "manifest.json", destination / "manifest.json")
    expected = {entry["path"]: entry for entry in files}
    verified = set()
    import hashlib

    with tarfile.open(archive, "r|") as tar:
        for member in tar:
            if not member.isfile():
                continue
            relative = "/".join(Path(member.name).parts[1:])
            if relative == "manifest.json":
                stream = tar.extractfile(member)
                assert stream is not None
                assert stream.read() == (KIT / "manifest.json").read_bytes()
                continue
            assert relative in expected, relative
            stream = tar.extractfile(member)
            assert stream is not None
            actual = hashlib.sha256()
            with stream:
                while chunk := stream.read(1024 * 1024):
                    actual.update(chunk)
            assert actual.hexdigest() == expected[relative]["sha256"], relative
            assert member.size == expected[relative]["bytes"]
            verified.add(relative)
    assert verified == set(expected)
    verification = {
        "status": "PASS",
        "archive_sha256": sha,
        "archive_bytes": archive.stat().st_size,
        "verified_files": len(verified),
    }
    (WORK / "source-kit-verification.json").write_text(
        json.dumps(verification, indent=2), encoding="utf-8"
    )
    shutil.copy2(WORK / "source-kit-verification.json", destination / "verification.json")
    print(f"SOURCE KIT {archive.stat().st_size} bytes SHA256 {sha}", flush=True)


if __name__ == "__main__":
    {"stage": stage, "kit": kit, "preserve": preserve}[sys.argv[1]]()
