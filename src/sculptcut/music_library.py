"""Reproducible local shuffled soundtracks with bounded two-input crossfades."""

import json
import math
import os
import random
import tempfile
from pathlib import Path
from threading import Event

from sculptcut.domain import ProjectSettings
from sculptcut.media import Cancelled, MediaError, discover, run_process
from sculptcut.music_selection import energetic_start, recent_tracks
from sculptcut.project import atomic_write, cache_key, fingerprint

EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".opus", ".wma"}


def library_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        raise MediaError(f"Music folder does not exist: {folder}")
    files = sorted(
        (p.resolve() for p in folder.iterdir() if p.is_file() and p.suffix.lower() in EXTENSIONS),
        key=lambda p: str(p).casefold(),
    )
    if not files:
        raise MediaError("Music folder contains no supported audio files")
    return files


def music_signature(settings: ProjectSettings) -> str:
    if settings.music_folder:
        return cache_key(
            "music-library-3-selection-mode",
            settings.music_seed,
            settings.music_selection_mode,
            settings.music_fade_seconds,
            [(str(p), fingerprint(p)) for p in library_files(settings.music_folder)],
        )
    return fingerprint(settings.music_path) if settings.music_path else ""


def audio_duration(path: Path, cancel: Event | None) -> float:
    info = json.loads(
        run_process(
            [
                discover("ffprobe"),
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=duration:format=duration",
                "-of",
                "json",
                str(path),
            ],
            cancel,
        )
    )
    if not info.get("streams"):
        raise MediaError(f"Music file has no audio stream: {path}")
    try:
        duration = float(info["streams"][0].get("duration", info.get("format", {}).get("duration")))
    except (ValueError, TypeError) as exc:
        raise MediaError(f"Cannot determine music duration: {path}") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise MediaError(f"Invalid music duration: {path}")
    return duration


def soundtrack(
    root: Path, settings: ProjectSettings, duration: float, cancel: Event | None = None
) -> Path | None:
    if not settings.music_folder:
        return settings.music_path
    if cancel and cancel.is_set():
        raise Cancelled("Music assembly cancelled")
    signature = music_signature(settings)
    key = cache_key("soundtrack-3-selection-mode", signature, duration)
    directory = root / "music"
    directory.mkdir(parents=True, exist_ok=True)
    output, manifest = directory / f"{key}.flac", directory / f"{key}.json"
    if output.is_file() and manifest.is_file():
        try:
            state = json.loads(manifest.read_text(encoding="utf-8"))
            if state["fingerprint"] == fingerprint(output):
                return output
        except (OSError, ValueError, KeyError):
            pass
    files = library_files(settings.music_folder)
    history = recent_tracks(directory)
    rng = random.Random(settings.music_seed)
    queue: list[Path] = []
    tracks: list[dict[str, str | float]] = []
    durations: dict[Path, float] = {}
    elapsed = 0.0
    last: Path | None = None
    with tempfile.TemporaryDirectory(prefix="mix-", dir=directory) as folder:
        temporary = Path(folder)
        mixed: Path | None = None
        while elapsed < duration - 0.000001:
            if cancel and cancel.is_set():
                raise Cancelled("Music assembly cancelled")
            if len(tracks) >= 256:
                raise MediaError("Too many short music tracks; use longer tracks")
            if not queue:
                queue = files.copy()
                rng.shuffle(queue)
                queue.sort(
                    key=lambda p: history.index(str(p)) if str(p) in history else -1,
                    reverse=True,
                )
                if len(queue) > 1 and queue[-1] == last:
                    queue[0], queue[-1] = queue[-1], queue[0]
            path = queue.pop()
            last = path
            if str(path) in history:
                history.remove(str(path))
            history.append(str(path))
            if path not in durations:
                durations[path] = audio_duration(path, cancel)
            offset = (
                energetic_start(path, durations[path], duration - elapsed, temporary, cancel)
                if settings.music_selection_mode == "dynamic"
                else 0.0
            )
            length = durations[path] - offset
            fade = min(settings.music_fade_seconds, length / 2, elapsed / 2) if mixed else 0
            length = min(length, duration - elapsed + fade)
            track = temporary / f"track-{len(tracks)}.flac"
            run_process(
                [
                    discover("ffmpeg"),
                    "-v",
                    "error",
                    "-nostdin",
                    "-xerror",
                    "-ss",
                    f"{offset:.6f}",
                    "-i",
                    str(path),
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-af",
                    f"atrim=duration={length:.9f},asetpts=PTS-STARTPTS,"
                    "loudnorm=I=-16:TP=-2:LRA=11,aresample=48000,"
                    "aformat=sample_fmts=s16:channel_layouts=stereo",
                    "-c:a",
                    "flac",
                    str(track),
                ],
                cancel,
            )
            if mixed:
                joined = temporary / f"mix-{len(tracks)}.flac"
                transition = (
                    f"acrossfade=d={fade:.9f}:c1=tri:c2=tri" if fade > 0 else "concat=n=2:v=0:a=1"
                )
                run_process(
                    [
                        discover("ffmpeg"),
                        "-v",
                        "error",
                        "-nostdin",
                        "-xerror",
                        "-i",
                        str(mixed),
                        "-i",
                        str(track),
                        "-filter_complex",
                        transition,
                        "-c:a",
                        "flac",
                        str(joined),
                    ],
                    cancel,
                )
                mixed.unlink()
                track.unlink()
                mixed = joined
            else:
                mixed = track
            tracks.append(
                {
                    "path": str(path),
                    "source_start_seconds": offset,
                    "start_seconds": elapsed - fade,
                    "duration_seconds": length,
                    "crossfade_seconds": fade,
                }
            )
            elapsed += length - fade
        assert mixed is not None
        if music_signature(settings) != signature:
            raise MediaError("Music folder changed during assembly; retry")
        os.replace(mixed, output)
        atomic_write(
            manifest,
            json.dumps(
                {
                    "seed": settings.music_seed,
                    "selection_mode": settings.music_selection_mode,
                    "tracks": tracks,
                    "fingerprint": fingerprint(output),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    return output
