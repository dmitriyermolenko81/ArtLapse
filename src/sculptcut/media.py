"""Bounded subprocess execution and presentation-time media metadata."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from threading import Event

from sculptcut.runtime_paths import environment_value, portable_root


class MediaError(RuntimeError):
    """A media command failed or produced unusable metadata."""


class Cancelled(MediaError):
    """The caller cancelled a media operation."""


def discover(name: str, configured: Path | None = None) -> str:
    portable = portable_root()
    if portable is not None and configured is None:
        candidate = portable / "bin" / f"{name}.exe"
        if not candidate.is_file():
            raise MediaError(
                f"Portable component missing: {candidate}; unpack the complete archive"
            )
        return str(candidate)
    explicit = configured or environment_value(name.upper())
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise MediaError(f"Configured {name} does not exist: {path}")
        return str(path.resolve())
    for directory in (Path(sys.executable).parent, Path(sys.executable).parent / "bin"):
        candidate = directory / f"{name}.exe"
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(name)
    if not found:
        raise MediaError(f"{name} not found; set ARTLAPSE_{name.upper()} or PATH")
    return found


def run_process(
    args: Sequence[str],
    cancel: Event | None = None,
    progress: Callable[[str], None] | None = None,
    heartbeat: Callable[[], None] | None = None,
    diagnostic_progress: Callable[[str], None] | None = None,
) -> str:
    """Spool output to disk; polling permits cancellation without pipe deadlocks.

    Stdout is limited to 8 MiB (metadata only); stderr diagnostics to its last 64 KiB.
    For FFmpeg progress, pass -progress pipe:1; callback receives appended text.
    """
    if cancel is not None and cancel.is_set():
        raise Cancelled("Cancelled before launch")
    temporary = tempfile.TemporaryDirectory(prefix="sculptcut-process-")
    try:
        return _execute(
            args, cancel, progress, Path(temporary.name), heartbeat, diagnostic_progress,
        )
    finally:
        _cleanup_process_directory(temporary)


def _cleanup_process_directory(temporary: tempfile.TemporaryDirectory[str]) -> None:
    # Windows can retain an inherited stdout/stderr handle briefly after taskkill
    # returns. Python 3.10 may turn that sharing violation into WinError 267.
    # Retry for at most one second; persistent or unrelated errors still surface.
    for attempt in range(21):
        try:
            temporary.cleanup()
            return
        except OSError as exc:
            if os.name != "nt" or exc.winerror not in (5, 32, 267) or attempt == 20:
                raise
            time.sleep(0.05)


def _execute(
    args: Sequence[str],
    cancel: Event | None,
    progress: Callable[[str], None] | None,
    directory: Path,
    heartbeat: Callable[[], None] | None = None,
    diagnostic_progress: Callable[[str], None] | None = None,
) -> str:
    with (directory / "stdout").open("w+b") as output, (directory / "stderr").open("w+b") as errors:
        process = subprocess.Popen(
            list(args),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=errors,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        offset = 0
        error_offset = 0
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    raise Cancelled("Media operation cancelled")
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    pass
                if heartbeat:
                    heartbeat()
                if progress:
                    with (directory / "stdout").open("rb") as reader:
                        reader.seek(offset)
                        chunk = reader.read(65536)
                    offset += len(chunk)
                    if chunk:
                        progress(chunk.decode("utf-8", errors="replace"))
                if diagnostic_progress:
                    with (directory / "stderr").open("rb") as reader:
                        reader.seek(error_offset)
                        chunk = reader.read(65536)
                    error_offset += len(chunk)
                    if chunk:
                        diagnostic_progress(chunk.decode("utf-8", errors="replace"))
                if process.poll() is not None:
                    break
        finally:
            if process.poll() is None:
                if os.name == "nt":
                    # A configured executable can be a launcher; stop its children too
                    # before removing the temporary files inherited by those children.
                    taskkill = Path(os.environ["SystemRoot"]) / "System32" / "taskkill.exe"
                    subprocess.run(
                        [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                        check=False,
                    )
                if process.poll() is None:
                    process.kill()
            process.wait()
        errors.seek(0, 2)
        errors.seek(max(0, errors.tell() - 65536))
        diagnostic = errors.read().decode("utf-8", errors="replace")
        if process.returncode:
            raise MediaError(f"Process exited {process.returncode}: {diagnostic}")
        output.seek(0)
        data = output.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            if progress:
                return ""
            raise MediaError("Metadata output exceeds 8 MiB")
        return data.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class MediaInfo:
    path: str
    duration_us: int | None
    width: int
    height: int
    fps_num: int
    fps_den: int
    time_base: str
    start_time: int
    stream_index: int
    rotation: int
    audio_streams: list[int]


def microseconds(value: object) -> int | None:
    try:
        return round(Fraction(str(value)) * 1_000_000)
    except (ValueError, ZeroDivisionError):
        return None


def parse_probe(data: str, path: Path) -> MediaInfo:
    payload = json.loads(data)
    streams = payload.get("streams", [])
    videos = [
        s
        for s in streams
        if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic", 0)
    ]
    if not videos:
        raise MediaError("No video stream")
    stream = videos[0]
    container = payload.get("format", {})
    duration = microseconds(stream.get("duration"))
    if duration is None:
        # Matroska/WebM reports per-stream end timestamps in DURATION tags.
        # Container duration may include an audio-only Opus padding tail.
        tagged = str(stream.get("tags", {}).get("DURATION", ""))
        try:
            hours, minutes, seconds = tagged.split(":")
            end = microseconds(int(hours) * 3600 + int(minutes) * 60 + Fraction(seconds))
            start = microseconds(stream.get("start_time", 0)) or 0
            if end is not None and end > start:
                duration = end - start
        except (ValueError, ZeroDivisionError):
            pass
    if duration is None:
        duration = microseconds(container.get("duration"))
    if duration is not None and duration <= 0:
        duration = None
    fps = Fraction(0)
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            fps = Fraction(stream.get(key, "0/1"))
        except (ValueError, ZeroDivisionError):
            continue
        if fps > 0:
            break
    rotation = int(float(stream.get("tags", {}).get("rotate", 0)))
    for side in stream.get("side_data_list", []):
        if "rotation" in side:
            rotation = int(side["rotation"])
    return MediaInfo(
        str(path.resolve()),
        duration,
        int(stream.get("width", 0)),
        int(stream.get("height", 0)),
        fps.numerator,
        fps.denominator,
        stream.get("time_base", "0/1"),
        microseconds(stream.get("start_time", container.get("start_time", 0))) or 0,
        int(stream["index"]),
        rotation,
        [int(s["index"]) for s in streams if s.get("codec_type") == "audio"],
    )


def probe(path: Path, cancel: Event | None = None) -> MediaInfo:
    if not path.is_file():
        raise MediaError(f"Source does not exist: {path}")
    return parse_probe(
        run_process(
            [
                discover("ffprobe"),
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path.resolve()),
            ], cancel
        ),
        path,
    )


def doctor() -> dict[str, object]:
    results: dict[str, object] = {}
    for name in ("ffmpeg", "ffprobe"):
        binary = discover(name)
        results[name] = run_process([binary, "-version"]).splitlines()[0]
    for encoder in ("libx264", "h264_nvenc"):
        try:
            run_process(
                [
                    discover("ffmpeg"),
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=s=640x360:r=24",
                    "-frames:v",
                    "2",
                    "-c:v",
                    encoder,
                    "-f",
                    "null",
                    "-",
                ]
            )
            results[encoder] = True
        except MediaError as exc:
            results[encoder] = False
            results[f"{encoder}_error"] = str(exc)
    return results


def probe_dict(path: Path) -> dict[str, object]:
    return asdict(probe(path))
