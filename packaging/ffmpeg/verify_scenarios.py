# SPDX-License-Identifier: GPL-3.0-or-later
"""Real cross-build media checks using baseline-generated inputs and candidate tools."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pefile
from prepare_inputs import digest

from sculptcut.audio import add_music
from sculptcut.domain import ProjectSettings
from sculptcut.media import discover, doctor, run_process
from sculptcut.planning import build_plan
from sculptcut.project import create_project
from sculptcut.proxy import make_proxies
from sculptcut.rendering import render, verify_output

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "artifacts/ffmpeg-own-202"
BASE = ROOT / "dist/ArtLapse-201-TransparentLogo/bin/ffmpeg.exe"
BIN = WORK / "installed/bin"


def main() -> None:
    os.environ["PATH"] = str(BIN) + os.pathsep + os.environ["PATH"]
    for key in ("ARTLAPSE_FFMPEG", "ARTLAPSE_FFPROBE", "SCULPTCUT_FFMPEG", "SCULPTCUT_FFPROBE"):
        os.environ.pop(key, None)
    folder = WORK / (sys.argv[1] if len(sys.argv) > 1 else "scenarios")
    folder.mkdir(exist_ok=False)
    result: dict[str, object] = {"doctor": doctor(), "inputs": [], "audio": [], "imports": {}}
    report = folder / "report.json"

    def save() -> None:
        report.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    ffmpeg = discover("ffmpeg")
    assert Path(ffmpeg).resolve() == (BIN / "ffmpeg.exe").resolve()
    required = {
        "filters": {
            "trim",
            "setpts",
            "settb",
            "fps",
            "scale",
            "pad",
            "crop",
            "format",
            "setsar",
            "split",
            "overlay",
            "boxblur",
            "xfade",
            "concat",
            "tpad",
            "select",
            "zoompan",
            "drawbox",
            "color",
            "testsrc2",
            "lutrgb",
            "atrim",
            "asetpts",
            "loudnorm",
            "aresample",
            "apad",
            "afade",
            "acrossfade",
            "sine",
        },
        "encoders": {
            "libx264",
            "h264_nvenc",
            "aac",
            "flac",
            "mpeg4",
            "png",
            "mjpeg",
            "libvpx",
            "libvpx-vp9",
            "libopus",
        },
        "decoders": {
            "h264",
            "hevc",
            "vp8",
            "vp9",
            "libdav1d",
            "mp3float",
            "aac",
            "flac",
            "vorbis",
            "opus",
            "wmav2",
            "pcm_s16le",
            "png",
            "mjpeg",
            "webp",
        },
    }
    for category, names in required.items():
        listing = run_process([ffmpeg, "-hide_banner", f"-{category}"])
        available = set(re.findall(r"^\s*[.A-Z|]{2,6}\s+(\S+)", listing, re.MULTILINE))
        assert names <= available, (category, names - available)
        (folder / f"{category}.txt").write_text(listing, encoding="utf-8")
    result["capabilities"] = {k: sorted(v) for k, v in required.items()}
    imports: dict[str, list[str]] = {}
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        pe = pefile.PE(str(BIN / name))
        assert pe.FILE_HEADER.Machine == 0x8664
        imports[name] = [entry.dll.decode() for entry in pe.DIRECTORY_ENTRY_IMPORT]
        assert not any(
            re.search(r"(msys-|libwinpthread|libc\+\+|libgcc|avcodec|x264)", i, re.I)
            for i in imports[name]
        )
        pe.close()
    result["imports"] = imports
    save()
    cases = [
        ("mp4", "libx264", []),
        ("mkv", "libx265", ["-x265-params", "pools=2:frame-threads=2"]),
        ("mov", "mjpeg", []),
        ("webm", "libvpx", ["-deadline", "realtime"]),
        ("webm", "libvpx-vp9", ["-deadline", "realtime"]),
        ("mkv", "libaom-av1", ["-cpu-used", "8"]),
    ]
    outputs: list[dict[str, object]] = []
    for index, (extension, codec, options) in enumerate(cases):
        print(f"video {codec}/{extension}", flush=True)
        source = folder / f"исходник {index}.{extension}"
        run_process(
            [
                str(BASE),
                "-v",
                "error",
                "-nostdin",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=160x96:r=10:d=2",
                "-c:v",
                codec,
                "-threads",
                "2",
                *options,
                str(source),
            ]
        )
        original = digest(source)
        project_root = folder / f"project-{index}"
        project = create_project(project_root, [source]).model_copy(
            update={"settings": ProjectSettings(proxy_width=160, proxy_fps=5)}
        )
        make_proxies(project_root, project)
        plan = build_plan(project, [], 1)
        cpu = render(project_root, project, plan, folder / f"cpu-{index}.mp4", encoder="libx264")
        preview = render(project_root, project, plan, folder / f"preview-{index}.mp4", preview=True)
        for output in (cpu, preview):
            verify_output(output, 30, 30)
            run_process([ffmpeg, "-v", "error", "-xerror", "-i", str(output), "-f", "null", "-"])
        assert digest(source) == original
        outputs.append(
            {
                "source": source.name,
                "codec": codec,
                "sha256": original,
                "cpu": cpu.name,
                "preview": preview.name,
                "frames": 30,
            }
        )
        if index == 0:
            status: list[str] = []
            gpu = render(
                project_root,
                project,
                plan,
                folder / "nvenc.mp4",
                encoder="h264_nvenc",
                encoder_status=status.append,
            )
            assert any("NVENC" in value for value in status), status
            assert not any("возврат" in value for value in status), status
            verify_output(gpu, 30, 30)
            run_process([ffmpeg, "-v", "error", "-xerror", "-i", str(gpu), "-f", "null", "-"])
            result["nvenc"] = {"status": status, "frames": 30, "output": gpu.name}
        result["inputs"] = outputs
        save()
    music_cases = [
        ("mp3", "libmp3lame"),
        ("wav", "pcm_s16le"),
        ("flac", "flac"),
        ("m4a", "aac"),
        ("ogg", "libvorbis"),
        ("aac", "aac"),
        ("opus", "libopus"),
        ("wma", "wmav2"),
    ]
    audio: list[dict[str, str]] = []
    for extension, codec in music_cases:
        print(f"music {extension}", flush=True)
        source = folder / f"музыка.{extension}"
        run_process(
            [
                str(BASE),
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=2",
                "-c:a",
                codec,
                str(source),
            ]
        )
        original = digest(source)
        output = folder / f"music-{extension}.mp4"
        add_music(folder / "cpu-0.mp4", source, output, 1, False, 0.1)
        verify_output(output, 30, 30, expected_audio=True)
        run_process([ffmpeg, "-v", "error", "-xerror", "-i", str(output), "-f", "null", "-"])
        assert digest(source) == original
        audio.append({"format": extension, "sha256": original, "output": output.name})
        result["audio"] = audio
        save()
    result["gpu"] = subprocess.check_output(
        [
            str(Path(os.environ["SystemRoot"]) / "System32/nvidia-smi.exe"),
            "--query-gpu=name,driver_version",
            "--format=csv,noheader",
        ],
        creationflags=subprocess.CREATE_NO_WINDOW,
        text=True,
    ).strip()
    result["status"] = "PASS"
    result["probe"] = json.loads(
        run_process(
            [
                str(BIN / "ffprobe.exe"),
                "-v",
                "error",
                "-count_frames",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(folder / "cpu-0.mp4"),
            ]
        )
    )
    save()
    print("SCENARIOS PASS", flush=True)


if __name__ == "__main__":
    main()
