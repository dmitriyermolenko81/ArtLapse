"""Optional local music assembly. Original source audio is never mapped."""

import json
from pathlib import Path
from threading import Event

from sculptcut.media import MediaError, discover, run_process


def validate_music(path: Path) -> None:
    if not path.is_file():
        raise MediaError(f"Music file does not exist: {path}")
    result = json.loads(
        run_process(
            [
                discover("ffprobe"),
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=index",
                "-of",
                "json",
                str(path.resolve()),
            ]
        )
    )
    if not result.get("streams"):
        raise MediaError("Music file has no audio stream")


def music_filter(duration: float, fade_seconds: float) -> str:
    """The same music timing, normalization and fades for export and inspection."""
    fade = min(fade_seconds, duration / 2)
    filters = (
        f"atrim=duration={duration:.9f},asetpts=PTS-STARTPTS,"
        "loudnorm=I=-16:TP=-2:LRA=11,aresample=48000,"
        f"apad,atrim=duration={duration:.9f}"
    )
    if fade > 0:
        filters += (
            f",afade=t=in:st=0:d={fade:.9f},afade=t=out:st={duration - fade:.9f}:d={fade:.9f}"
        )
    return filters


def add_music(
    video: Path,
    music: Path,
    output: Path,
    duration: float,
    loop: bool,
    fade_seconds: float,
    cancel: Event | None = None,
) -> None:
    validate_music(music)
    if output.exists() or output.resolve() in (video.resolve(), music.resolve()):
        raise ValueError("Music output must be a new path")
    loop_args = ["-stream_loop", "-1"] if loop else []
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-xerror",
            "-nostdin",
            "-i",
            str(video),
            *loop_args,
            "-i",
            str(music.resolve()),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-af",
            music_filter(duration, fade_seconds),
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-t",
            f"{duration:.9f}",
            "-movflags",
            "+faststart",
            str(output),
        ],
        cancel,
    )
