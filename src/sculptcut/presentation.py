"""A complete turnaround clip fitted to a short presentation before/after the montage."""

import tempfile
from pathlib import Path
from threading import Event

from sculptcut.domain import ProjectSettings
from sculptcut.media import discover, probe, run_process
from sculptcut.project import fingerprint
from sculptcut.shorts_layout import SHORTS_PRESETS, SQUARE_BACKGROUND, uses_square


def presentation_frames(settings: ProjectSettings, fps: int) -> int:
    return max(1, round(settings.presentation_seconds * fps)) if settings.presentation_video else 0


def presentation_signature(settings: ProjectSettings) -> str:
    video = settings.presentation_video
    if video is None:
        return ""
    if not video.is_file():
        raise ValueError(f"Видео-презентация не найдено: {video}")
    return ":".join(("presentation-1", fingerprint(video), settings.presentation_position,
                     f"{settings.presentation_seconds:.6f}"))


def attach_presentation(
    body: Path, output: Path, settings: ProjectSettings, *, width: int, height: int,
    fps: int, encoder: str, cancel: Event | None = None,
) -> None:
    """Encode only the short clip; concatenate the normalized H.264 streams losslessly."""
    video = settings.presentation_video
    if video is None:
        raise ValueError("Presentation video is not configured")
    if output.exists() or output.resolve() in (body.resolve(), video.resolve()):
        raise ValueError("Presentation output must be a new path")
    info = probe(video, cancel)
    if not info.duration_us:
        raise ValueError("Не удалось определить длительность видео-презентации")
    count = presentation_frames(settings, fps)
    ratio = count * 1_000_000 / (fps * info.duration_us)
    composition = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
        f"force_divisible_by=2:flags=lanczos,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
    )
    if uses_square(settings):
        composition = (
            f"scale={width}:{width}:force_original_aspect_ratio=decrease:"
            f"force_divisible_by=2:flags=lanczos,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={SQUARE_BACKGROUND},"
        )
    elif settings.preset in SHORTS_PRESETS:
        composition = (
            f"split[back][front];[back]scale={width}:{height}:"
            f"force_original_aspect_ratio=increase,crop={width}:{height},"
            "boxblur=30:2[blur];[front]"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
            "force_divisible_by=2:flags=lanczos[sharp];"
            "[blur][sharp]overlay=(W-w)/2:(H-h)/2,"
        )
    quality = (["-preset", "p4", "-rc", "vbr", "-cq", "18", "-b:v", "0"]
               if encoder == "h264_nvenc" else ["-preset", "fast", "-crf", "18"])
    with tempfile.TemporaryDirectory(prefix="presentation-", dir=output.parent) as folder:
        scratch = Path(folder)
        clip = scratch / "clip.mp4"
        script = scratch / "filter.txt"
        script.write_text(
            f"setpts=(PTS-STARTPTS)*{ratio:.12f},fps={fps},{composition}setsar=1,"
            f"tpad=stop_mode=clone:stop_duration={count / fps:.9f},"
            f"trim=end_frame={count},settb=1/{fps},setpts=N,format=yuv420p",
            encoding="utf-8",
        )
        run_process([
            discover("ffmpeg"), "-v", "error", "-nostdin", "-xerror",
            "-reinit_filter", "0", "-i", str(video), "-map", f"0:{info.stream_index}",
            "-an", "-filter_script:v", str(script), "-frames:v", str(count),
            "-fps_mode", "passthrough", "-enc_time_base", f"1:{fps}",
            "-c:v", encoder, *quality, "-pix_fmt", "yuv420p", str(clip),
        ], cancel)
        listing = scratch / "concat.txt"
        paths = [clip, body] if settings.presentation_position == "start" else [body, clip]
        listing.write_text("".join(
            "file '" + path.resolve().as_posix().replace("'", "'\\''") + "'\n" for path in paths
        ), encoding="utf-8")
        run_process([
            discover("ffmpeg"), "-v", "error", "-nostdin", "-xerror", "-f", "concat",
            "-safe", "0", "-i", str(listing), "-map", "0:v:0", "-an", "-c:v", "copy",
            "-movflags", "+faststart", str(output),
        ], cancel)
