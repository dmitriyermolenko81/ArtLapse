"""Deterministic small synthetic videos; no private recordings are copied."""

import json
import tempfile
from pathlib import Path

import cv2
import numpy as np

from sculptcut.media import discover, probe, run_process

KINDS = (
    "static",
    "pan",
    "zoom",
    "local",
    "ui",
    "mixed",
    "loop",
    "vfr",
    "rotation",
    "audio",
    "unicode",
)


def generate_fixture(path: Path, kind: str, duration: float = 3, fps: int = 10) -> Path:
    if kind not in KINDS or duration <= 0 or fps <= 0:
        raise ValueError("Invalid synthetic fixture configuration")
    if path.exists():
        raise ValueError("Fixture output already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    width, height = 192, 128
    rng = np.random.default_rng(42)
    base = np.full((height, width, 3), 50, dtype=np.uint8)
    texture = rng.integers(70, 210, (64, 96, 1), dtype=np.uint8)
    base[32:96, 48:144] = texture
    count = max(1, round(duration * fps))
    with tempfile.TemporaryDirectory(dir=path.parent, prefix="fixture-") as temporary:
        root = Path(temporary)
        raw = root / "frames.rgb"
        with raw.open("wb") as stream:
            for index in range(count):
                frame = base.copy()
                phase = index / max(1, count - 1)
                if kind in ("pan", "mixed"):
                    matrix = np.array([[1, 0, phase * 20], [0, 1, 0]], dtype=np.float32)
                    frame = cv2.warpAffine(
                        frame, matrix, (width, height), borderValue=(50, 50, 50)
                    ).astype(np.uint8)
                if kind == "zoom":
                    matrix = cv2.getRotationMatrix2D((96, 64), 0, 1 + phase * 0.2).astype(
                        np.float32
                    )
                    frame = cv2.warpAffine(
                        frame, matrix, (width, height), borderValue=(50, 50, 50)
                    ).astype(np.uint8)
                if kind in ("local", "mixed"):
                    cv2.circle(frame, (96, 64), 2 + round(phase * 16), (220, 160, 100), -1)
                if kind == "ui":
                    frame[:20, : 20 + round(phase * 70)] = (100, 180, 220)
                if kind == "loop" and 0.3 < phase < 0.7:
                    frame[40:88, 56:136] = (210, 100, 80)
                stream.write(frame.tobytes())
        encoded = root / "encoded.mp4" if kind == "rotation" else path
        args = [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            f"{width}x{height}",
            "-r",
            str(fps),
            "-i",
            str(raw),
        ]
        if kind == "audio":
            args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}", "-shortest"]
        if kind == "vfr":
            args += [
                "-vf",
                f"select='if(lt(t,{duration / 2}),1,not(mod(n,2)))'",
                "-fps_mode",
                "vfr",
            ]
        args += ["-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", str(encoded)]
        run_process(args)
        if kind == "rotation":
            run_process(
                [
                    discover("ffmpeg"),
                    "-v",
                    "error",
                    "-display_rotation:v:0",
                    "90",
                    "-i",
                    str(encoded),
                    "-c",
                    "copy",
                    str(path),
                ]
            )
    return path


def generate_suite(root: Path) -> Path:
    if root.exists() and any(root.iterdir()):
        raise ValueError("Fixture suite directory must be empty")
    root.mkdir(parents=True, exist_ok=True)
    clips = []
    labels = {
        "static": "STATIC",
        "pan": "CAMERA",
        "zoom": "CAMERA",
        "local": "SCULPT",
        "ui": "INTERFACE",
        "mixed": "MIXED",
        "loop": "UNKNOWN",
    }
    for kind in KINDS:
        name = "кириллица с пробелом.mp4" if kind == "unicode" else f"{kind}.mp4"
        path = generate_fixture(root / name, kind, 6 if kind == "loop" else 3)
        info = probe(path)
        clips.append(
            {
                "id": kind,
                "path": name,
                "duration_us": info.duration_us,
                "segments": [
                    {"start_us": 0, "end_us": info.duration_us, "kind": labels.get(kind, "UNKNOWN")}
                ],
                "annotation_origin": "synthetic",
                "reviewed": True,
            }
        )
    document = {
        "schema_version": 1,
        "clips": clips,
        "multi_source_order": ["static", "local", "pan"],
        "note": "Synthetic mechanics only; these labels do not establish real artistic quality.",
    }
    destination = root / "annotations.json"
    destination.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return destination
