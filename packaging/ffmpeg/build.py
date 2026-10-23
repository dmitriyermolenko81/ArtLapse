# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the isolated Windows x64 candidate. W: must map to the build workspace."""

import difflib
import os
import shutil
import subprocess
import sys
from pathlib import Path

WORK = Path("W:/")
SOURCES = WORK / "sources"
PREFIX = WORK / "prefix"
TOOLS = WORK / "tools"
LOGS = WORK / "logs"
ENV = os.environ.copy()
ENV["PATH"] = os.pathsep.join(
    str(p)
    for p in (
        TOOLS / "llvm-mingw-20261020-ucrt-x86_64/bin",
        TOOLS / "nasm-3.01",
        TOOLS / "python/bin",
        PREFIX / "bin",
        Path("C:/Program Files/Git/usr/bin"),
        Path("C:/Program Files/Git/mingw64/bin"),
        Path(os.environ["SystemRoot"]) / "System32",
    )
)
ENV["PYTHONPATH"] = str(TOOLS / "python")
ENV["CC"] = "x86_64-w64-mingw32-clang"
ENV["CXX"] = "x86_64-w64-mingw32-clang++"
ENV["AR"] = "llvm-ar"
ENV["PKG_CONFIG_PATH"] = str(PREFIX / "lib/pkgconfig")
ENV["PKG_CONFIG_LIBDIR"] = str(PREFIX / "lib/pkgconfig")
ENV["CFLAGS"] = "-O2 -g0"
ENV["CXXFLAGS"] = "-O2 -g0"
ENV["SOURCE_DATE_EPOCH"] = "1781664453"
ENV["TZ"] = "UTC"
MAKE = "mingw32-make"
BASH = "C:/Program Files/Git/usr/bin/bash.exe"


def run(name: str, args: list[str], cwd: Path = WORK) -> None:
    print(name, flush=True)
    executable = shutil.which(args[0], path=ENV["PATH"])
    if executable is None:
        raise FileNotFoundError(args[0])
    args = [executable, *args[1:]]
    with (LOGS / f"{name}.log").open("a", encoding="utf-8") as output:
        output.write(repr(args) + "\n")
        output.flush()
        subprocess.run(
            args,
            cwd=cwd,
            env=ENV,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=True,
        )


def shell(name: str, command: str, cwd: Path) -> None:
    run(name, [BASH, "-c", command], cwd)


def meson(name: str, source: str, options: list[str]) -> None:
    build = WORK / "build" / name
    reconfigure = ["--reconfigure"] if (build / "meson-private/coredata.dat").exists() else []
    run(
        f"{name}-configure",
        [
            sys.executable,
            "-m",
            "mesonbuild.mesonmain",
            "setup",
            str(build),
            str(SOURCES / source),
            f"--prefix={PREFIX}",
            "--libdir=lib",
            "--default-library=static",
            "--buildtype=release",
            "--wrap-mode=nodownload",
            *reconfigure,
            *options,
        ],
    )
    run(f"{name}-make", ["ninja", "-C", str(build), "-j4"])
    run(f"{name}-install", ["ninja", "-C", str(build), "install"])


def core_dependencies() -> None:
    meson(
        "opus-1.6.1",
        "opus-1.6.1",
        ["-Dextra-programs=disabled", "-Dtests=disabled", "-Ddocs=disabled"],
    )
    zlib = SOURCES / "zlib-1.3.2"
    run(
        "zlib-1.3.2-make",
        [
            MAKE,
            "-f",
            "win32/Makefile.gcc",
            "-j4",
            "libz.a",
            "CC=x86_64-w64-mingw32-clang",
            "AR=llvm-ar",
        ],
        zlib,
    )
    for name in ("zlib.h", "zconf.h"):
        shutil.copy2(zlib / name, PREFIX / "include" / name)
    shutil.copy2(zlib / "libz.a", PREFIX / "lib/libz.a")


def main() -> None:
    PREFIX.mkdir(exist_ok=True)
    (WORK / "build").mkdir(exist_ok=True)
    stage = sys.argv[1] if len(sys.argv) > 1 else "deps"
    if stage == "refresh":
        core_dependencies()
        return
    if stage == "deps":
        run(
            "tool-versions",
            [
                BASH,
                "-c",
                "clang --version; mingw32-make --version; nasm -v; ninja --version; bash --version",
            ],
        )
        meson("pkgconf", "pkgconf-2.5.1", ["-Dtests=disabled"])
        shutil.copy2(PREFIX / "bin/pkgconf.exe", PREFIX / "bin/pkg-config.exe")
        core_dependencies()
        # NASM's Windows file API cannot open the Unicode physical path resolved by Meson.
        # Relative includes preserve assembly optimizations and work after relocation.
        source = SOURCES / "dav1d-1.5.3/meson.build"
        old = source.read_text(encoding="utf-8")
        if "nasm_relative_src" not in old:
            new = (
                old.replace(
                    "    nasm_gen = generator(nasm,",
                    "    fs = import('fs')\n"
                    "    nasm_relative_src = fs.relative_to(dav1d_src_root / 'src', "
                    "meson.project_build_root())\n"
                    "    nasm_gen = generator(nasm,",
                )
                .replace(
                    "'-I', '@0@/src/'.format(dav1d_src_root),",
                    "'-I', nasm_relative_src + '/',",
                )
                .replace(
                    "'-I', '@0@/'.format(meson.current_build_dir()),",
                    "'-I', './',",
                )
            )
            patch = "".join(
                difflib.unified_diff(
                    old.splitlines(keepends=True),
                    new.splitlines(keepends=True),
                    fromfile="a/meson.build",
                    tofile="b/meson.build",
                    n=0,
                )
            )
            patch_dir = Path(__file__).resolve().parent / "patches"
            patch_dir.mkdir(exist_ok=True)
            (patch_dir / "dav1d-relative-nasm.patch").write_text(patch, encoding="utf-8")
            source.write_text(new, encoding="utf-8")
        meson("dav1d", "dav1d-1.5.3", ["-Denable_tools=false", "-Denable_tests=false"])
    if stage in ("deps", "deps-tail"):
        x264 = SOURCES / "x264-0480cb05fa188d37ae87e8f4fd8f1aea3711f7ee"
        shell(
            "x264-configure",
            "./configure --prefix=W:/prefix --enable-static "
            "--disable-cli --disable-opencl --host=x86_64-w64-mingw32 "
            "--cross-prefix=x86_64-w64-mingw32-",
            x264,
        )
        run("x264-make", [MAKE, "-j4"], x264)
        run("x264-install", [MAKE, "install"], x264)
        vpx = SOURCES / "libvpx-1.15.2"
        shell(
            "vpx-configure",
            "./configure --prefix=W:/prefix --target=x86_64-win64-gcc "
            "--disable-shared --enable-static --disable-examples --disable-tools "
            "--disable-docs --disable-unit-tests --enable-vp9-highbitdepth "
            "--disable-dependency-tracking",
            vpx,
        )
        # Native Windows Make does not interpret MSYS drive mount paths.
        for config in vpx.glob("*.mk"):
            text = config.read_text(encoding="utf-8")
            # Perl's config reader expects LF: CRLF would change "yes" into "yes\r".
            config.write_bytes(text.replace("/w/", "W:/").encode("utf-8"))
        # Upstream clean exceeds Windows' command-line limit. Remove only generated
        # files inside this isolated extraction, using native filesystem operations.
        generated = [*vpx.rglob("*.o"), *vpx.rglob("*.d")]
        generated.extend(
            vpx / name
            for name in (
                "vpx_scale_rtcd.h",
                "vpx_dsp_rtcd.h",
                "vp8_rtcd.h",
                "vp9_rtcd.h",
            )
        )
        for path in generated:
            if not path.resolve().is_relative_to(vpx.resolve()):
                raise ValueError(f"Generated file escapes libvpx build: {path}")
            path.unlink(missing_ok=True)
        run("vpx-make", [MAKE, "-j4"], vpx)
        run("vpx-install", [MAKE, "install"], vpx)
        run(
            "nvheaders-install",
            [MAKE, "PREFIX=W:/prefix", "install"],
            SOURCES / "nv-codec-headers-n13.0.19.0",
        )
    elif stage in ("ffmpeg", "ffmpeg-resume"):
        ffmpeg = SOURCES / "ffmpeg-8.1.2"
        # Windows' case-insensitive lookup makes the release VERSION file shadow
        # libc++'s <version>. Keep all filters and rename only release metadata.
        version = ffmpeg / "VERSION"
        renamed = ffmpeg / "FFMPEG_RELEASE_VERSION"
        version_script = ffmpeg / "ffbuild/version.sh"
        if version.exists():
            content = version.read_text(encoding="utf-8")
            old = version_script.read_text(encoding="utf-8")
            new = old.replace("cat VERSION ", "cat FFMPEG_RELEASE_VERSION ")
            if old == new or renamed.exists():
                raise ValueError("Unexpected FFmpeg release metadata")
            patch = "".join(
                difflib.unified_diff(
                    content.splitlines(keepends=True),
                    [],
                    "a/VERSION",
                    "/dev/null",
                    n=0,
                )
            )
            patch += "".join(
                difflib.unified_diff(
                    [],
                    content.splitlines(keepends=True),
                    "/dev/null",
                    "b/FFMPEG_RELEASE_VERSION",
                    n=0,
                )
            )
            patch += "".join(
                difflib.unified_diff(
                    old.splitlines(keepends=True),
                    new.splitlines(keepends=True),
                    "a/ffbuild/version.sh",
                    "b/ffbuild/version.sh",
                    n=0,
                )
            )
            patch_dir = Path(__file__).resolve().parent / "patches"
            patch_dir.mkdir(exist_ok=True)
            (patch_dir / "ffmpeg-release-version.patch").write_bytes(patch.encode("utf-8"))
            version.rename(renamed)
            version_script.write_bytes(new.encode("utf-8"))
        capture = ffmpeg / "libavfilter/vsrc_gfxcapture_winrt.cpp"
        old = capture.read_text(encoding="utf-8")
        if "#include <system_error>" not in old:
            new = old.replace("#include <string>\n", "#include <string>\n#include <system_error>\n")
            if old == new:
                raise ValueError("Unexpected FFmpeg capture includes")
            patch = "".join(
                difflib.unified_diff(
                    old.splitlines(keepends=True),
                    new.splitlines(keepends=True),
                    "a/libavfilter/vsrc_gfxcapture_winrt.cpp",
                    "b/libavfilter/vsrc_gfxcapture_winrt.cpp",
                    n=0,
                )
            )
            patch_dir = Path(__file__).resolve().parent / "patches"
            (patch_dir / "ffmpeg-system-error.patch").write_bytes(patch.encode("utf-8"))
            capture.write_bytes(new.encode("utf-8"))
        library = ffmpeg / "ffbuild/library.mak"
        old = library.read_text(encoding="utf-8")
        if "$(Q)echo $^ > $@.objs" in old:
            new = old.replace("$(Q)echo $^ > $@.objs", "$(file >$@.objs,$^)")
            new = new.replace(
                "$(Q)echo $$(filter %.o,$$^) > $$@.objs",
                "$$(file >$$@.objs,$$(filter %.o,$$^))",
            )
            patch = "".join(
                difflib.unified_diff(
                    old.splitlines(keepends=True),
                    new.splitlines(keepends=True),
                    "a/ffbuild/library.mak",
                    "b/ffbuild/library.mak",
                    n=0,
                )
            )
            patch_dir = Path(__file__).resolve().parent / "patches"
            (patch_dir / "ffmpeg-native-response.patch").write_bytes(patch.encode("utf-8"))
            library.write_bytes(new.encode("utf-8"))
        if stage == "ffmpeg-resume":
            run("ffmpeg-make", [MAKE, "-j4"], ffmpeg)
            run("ffmpeg-install", [MAKE, "install"], ffmpeg)
            return
        shell(
            "ffmpeg-configure",
            "./configure --prefix=W:/installed "
            "--target-os=mingw32 --arch=x86_64 --cc=x86_64-w64-mingw32-clang "
            "--cxx=x86_64-w64-mingw32-clang++ --ar=llvm-ar --ranlib=llvm-ranlib "
            "--nm=llvm-nm --strip=llvm-strip --pkg-config=pkg-config "
            "--pkg-config-flags=--static --extra-cflags=-IW:/prefix/include "
            "--extra-ldflags='-LW:/prefix/lib -static' --enable-gpl --enable-version3 "
            "--enable-static --disable-shared --disable-autodetect --disable-doc "
            "--disable-ffplay --disable-debug --enable-libx264 --enable-libvpx "
            "--enable-libopus --enable-libdav1d --enable-zlib --enable-ffnvcodec "
            "--enable-nvenc --enable-nvdec --enable-cuvid --enable-d3d11va "
            "--enable-dxva2 --enable-mediafoundation --extra-version=ArtLapse-202",
            ffmpeg,
        )
        run("ffmpeg-make", [MAKE, "-j4"], ffmpeg)
        run("ffmpeg-install", [MAKE, "install"], ffmpeg)
    else:
        raise ValueError(f"Unknown stage: {stage}")


if __name__ == "__main__":
    main()
