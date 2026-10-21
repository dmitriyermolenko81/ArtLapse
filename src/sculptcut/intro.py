"""Optional still-image intro shared by preview and original-source export."""

import tempfile
from pathlib import Path
from threading import Event

from sculptcut.domain import ProjectSettings
from sculptcut.media import discover, run_process
from sculptcut.presentation import presentation_frames, presentation_signature
from sculptcut.project import fingerprint
from sculptcut.shorts_layout import SQUARE_BACKGROUND, uses_square


def intro_signature(settings: ProjectSettings) -> str:
    presentation = presentation_signature(settings)
    image = settings.intro_image
    if image is None:
        return presentation
    if not image.is_file():
        raise ValueError(f"Файл заставки не найден: {image}")
    signature = ":".join(
        (
            "intro-1",
            fingerprint(image),
            f"{settings.intro_seconds:.6f}",
            f"{settings.intro_transition_seconds:.6f}",
        )
    )
    return signature + (":" + presentation if presentation else "")


def intro_frames(settings: ProjectSettings, fps: int, body_frames: int) -> tuple[int, int]:
    """Return still and overlap frame counts, or zeroes when no intro is selected."""
    if settings.intro_image is None:
        return 0, 0
    still = max(2, round(settings.intro_seconds * fps))
    overlap = min(still - 1, body_frames, max(1, round(settings.intro_transition_seconds * fps)))
    return still, overlap


def output_frame_count(settings: ProjectSettings, fps: int, body_frames: int) -> int:
    still, overlap = intro_frames(settings, fps, body_frames)
    return body_frames + still - overlap + presentation_frames(settings, fps)


def body_start_frame(settings: ProjectSettings, fps: int, body_frames: int) -> int:
    still, overlap = intro_frames(settings, fps, body_frames)
    before = presentation_frames(settings, fps) if settings.presentation_position == "start" else 0
    return still - overlap + before


def prepend_intro(
    video: Path,
    image: Path,
    output: Path,
    *,
    width: int,
    height: int,
    fps: int,
    body_frames: int,
    settings: ProjectSettings,
    encoder: str,
    cancel: Event | None = None,
) -> int:
    """Prepend a fitted still and crossfade into the already normalized video."""
    if output.exists() or output.resolve() in (video.resolve(), image.resolve()):
        raise ValueError("Intro output must be a new path")
    if not image.is_file():
        raise ValueError(f"Файл заставки не найден: {image}")
    still, overlap = intro_frames(settings, fps, body_frames)
    if not still:
        raise ValueError("Intro image is not configured")
    expected = body_frames + still - overlap
    fade = overlap / fps
    offset = (still - overlap) / fps
    quality = (
        ["-preset", "p4", "-rc", "vbr", "-cq", "18", "-b:v", "0"]
        if encoder == "h264_nvenc"
        else ["-preset", "fast", "-crf", "18"]
    )
    with tempfile.TemporaryDirectory(prefix="intro-", dir=output.parent) as scratch:
        script = Path(scratch) / "filter.txt"
        cover = (
            "[1:v]split[coverback][coverfront];"
            f"[coverback]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},boxblur=10:2[background];"
            f"[coverfront]scale={width}:{height}:force_original_aspect_ratio=decrease:"
            "force_divisible_by=2:flags=lanczos[foreground];"
            "[background][foreground]overlay=(W-w)/2:(H-h)/2,"
        )
        if uses_square(settings):
            cover = (
                f"[1:v]scale={width}:{width}:force_original_aspect_ratio=decrease:"
                "force_divisible_by=2:flags=lanczos,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={SQUARE_BACKGROUND},"
            )
        script.write_text(
            f"[0:v]fps={fps},settb=1/{fps},setpts=N,format=yuv420p[body];"
            f"{cover}"
            f"setsar=1,fps={fps},trim=end_frame={still},settb=1/{fps},"
            "setpts=N,format=yuv420p[intro];"
            f"[intro][body]xfade=transition=fade:duration={fade:.9f}:offset={offset:.9f},"
            f"fps={fps},settb=1/{fps},setpts=N,format=yuv420p[out]",
            encoding="utf-8",
        )
        run_process(
            [
                discover("ffmpeg"),
                "-v",
                "error",
                "-nostdin",
                "-xerror",
                "-i",
                str(video),
                "-loop",
                "1",
                "-framerate",
                str(fps),
                "-i",
                str(image.resolve()),
                "-filter_complex_script",
                str(script),
                "-map",
                "[out]",
                "-an",
                "-frames:v",
                str(expected),
                "-c:v",
                encoder,
                *quality,
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(output),
            ],
            cancel,
        )
    return expected
