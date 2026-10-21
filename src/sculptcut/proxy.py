"""CFR analysis proxies with explicit source-presentation-time mapping."""

import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import Protocol

import cv2

from sculptcut.domain import Project, SourceMedia
from sculptcut.media import MediaError, discover, microseconds, probe, run_process
from sculptcut.project import cache_get, cache_key, cache_put, validate_sources

PROXY_VERSION = 2


class FrameReader(Protocol):
    def get(self, prop: int) -> float: ...
    def isOpened(self) -> bool: ...


def validated_frame_count(reader: FrameReader, source: SourceMedia, fps: int) -> int:
    """Bound CFR analysis to real samples, keeping proxy creation's duration tolerance."""
    count = round(reader.get(cv2.CAP_PROP_FRAME_COUNT))
    if not reader.isOpened() or count <= 0:
        raise MediaError(f"Cannot open analysis proxy: {source.path}")
    duration = round(count * 1_000_000 / fps)
    if abs(duration - source.duration_us) > 2_000_000 / fps:
        raise MediaError(f"Analysis proxy duration mismatch: {source.path}")
    return count


def proxy_key(project: Project, source: SourceMedia) -> str:
    return cache_key(
        PROXY_VERSION, source.fingerprint, project.settings.proxy_width, project.settings.proxy_fps
    )


def proxy_path(root: Path, project: Project, source: SourceMedia) -> Path:
    return root / "proxy" / f"{source.id}-{proxy_key(project, source)[:16]}.mp4"


def make_proxies(
    root: Path,
    project: Project,
    cancel: Event | None = None,
    progress: Callable[[str, float], None] | None = None,
) -> list[Path]:
    validate_sources(root, project)
    outputs = []
    for source in project.sources:
        destination = proxy_path(root, project, source)
        key = proxy_key(project, source)
        cached = cache_get(root, "proxy", source.id, key)
        if cached and destination.is_file():
            outputs.append(destination)
            continue
        width = project.settings.proxy_width
        fps = project.settings.proxy_fps
        with tempfile.TemporaryDirectory(dir=root / "temp", prefix="proxy-") as directory:
            temporary = Path(directory) / "proxy.mp4"
            label = f"Creating and checking analysis proxy: {source.path.name}"
            if progress:
                progress(label, 0)
            pending = ""
            diagnostic_pending = ""
            first: int | None = None
            previous: int | None = None

            def encoded(chunk: str, label: str = label, duration: int = source.duration_us) -> None:
                nonlocal pending
                pending += chunk
                lines = pending.split("\n")
                pending = lines.pop()
                for line in lines:
                    if line.startswith("out_time_us=") and progress:
                        value = line.partition("=")[2]
                        if value.lstrip("-").isdigit():
                            progress(label, max(0, min(0.999, int(value) / duration)))

            def checked(chunk: str) -> None:
                nonlocal diagnostic_pending, first, previous
                diagnostic_pending += chunk
                lines = diagnostic_pending.split("\n")
                diagnostic_pending = lines.pop()
                for line in lines:
                    match = re.search(r"\bpts_time:([^\s]+)", line)
                    if not match:
                        continue
                    current = microseconds(match.group(1))
                    if current is None:
                        continue
                    if previous is not None and current <= previous:
                        raise MediaError(
                            "Non-increasing presentation timestamps; repair/reimport required"
                        )
                    if first is None:
                        first = current
                    previous = current

            # Preserve elapsed PTS gaps; fps duplicates samples across forward discontinuities.
            # The origin is the first displayed frame; rotation is applied by FFmpeg.
            run_process(
                [
                    discover("ffmpeg"),
                    "-v",
                    "info",
                    "-nostdin",
                    "-xerror",
                    "-progress",
                    "pipe:1",
                    "-copyts",
                    # Resolution changes must not reset STARTPTS or the fps timeline.
                    # scale handles changing input sizes and produces a fixed proxy size.
                    "-reinit_filter",
                    "0",
                    "-i",
                    str(source.path),
                    "-map",
                    f"0:{source.stream_index}",
                    "-an",
                    "-vf",
                    f"showinfo=checksum=0,setpts=PTS-STARTPTS,fps={fps},scale={width}:-2",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "28",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    str(temporary),
                ],
                cancel,
                progress=encoded,
                diagnostic_progress=checked,
            )
            if first is None:
                raise MediaError("No presentation timestamps")
            origin = first
            info = probe(temporary)
            if (
                info.duration_us is None
                or abs(info.duration_us - source.duration_us) > 2_000_000 / fps
            ):
                raise MediaError(
                    "Proxy duration mismatch; timestamp discontinuity requires review: "
                    f"source={source.duration_us} us, proxy={info.duration_us} us, "
                    f"first_pts={first} us, last_pts={previous} us; {source.path}"
                )
            os.replace(temporary, destination)
        cache_put(
            root,
            "proxy",
            source.id,
            key,
            {
                "path": str(destination),
                "fps": fps,
                "source_origin_us": origin,
                "mapping": "source_pts_us = source_origin_us + proxy_pts_us",
                "forward_gaps": "repeat_previous_frame",
                "version": PROXY_VERSION,
            },
        )
        outputs.append(destination)
        if progress:
            progress(label, 1)
    return outputs
