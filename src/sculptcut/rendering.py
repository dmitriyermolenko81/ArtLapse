"""Original-source chunk rendering, validation and atomic publication."""

import json
import logging
import math
import os
import tempfile
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from threading import Event

from sculptcut.audio import add_music, validate_music
from sculptcut.bookends import bookend_filter, final_frames
from sculptcut.domain import Project, Roi
from sculptcut.export_storage import metadata_path
from sculptcut.intro import active_intro_image, output_frame_count, prepend_intro
from sculptcut.jobs import finish_job, record_chunk, render_job, reusable_chunk
from sculptcut.media import Cancelled, MediaError, discover, doctor, parse_probe, probe, run_process
from sculptcut.music_library import soundtrack
from sculptcut.planning import RenderDecision, RenderPlan, validate_plan
from sculptcut.presentation import attach_presentation, presentation_frames
from sculptcut.project import atomic_write, validate_sources
from sculptcut.proxy import proxy_path
from sculptcut.render_batches import coalesce_chunks
from sculptcut.screen_cache import screen_check_cache
from sculptcut.shorts_layout import SHORTS_PRESETS, square_composition, uses_square
from sculptcut.subject_camera import camera_filter, still_frame

# Encode intermediates with more detail than the final delivery encode.
INTERMEDIATE_QUALITY = 16
FINAL_CRF = 18


def export_size(project: Project, plan: RenderPlan | None = None) -> tuple[int, int]:
    """Use the largest input canvas in display orientation, padded to even pixels."""
    if project.settings.preset in SHORTS_PRESETS:
        return 1080, 1920
    if not project.sources:
        raise ValueError("Cannot export a project without sources")
    if project.settings.preset == "timelapse" and project.settings.crop_mode == "viewport":
        if plan is None:
            raise ValueError("Viewport export size requires a render plan")
        sources = {source.id: source for source in project.sources}
        sizes = []
        for decision in plan.decisions:
            if decision.action == "DROP" or not isinstance(decision.crop, Roi):
                continue
            source = sources[decision.source_id]
            width, height = source.width, source.height
            if source.rotation % 180 == 90:
                width, height = height, width
            sizes.append(
                (
                    max(2, math.floor(width * decision.crop.width / 2) * 2),
                    max(2, math.floor(height * decision.crop.height / 2) * 2),
                )
            )
        if not sizes:
            raise ValueError("Viewport export requires cropped episodes")
        width, height = max(sizes, key=lambda size: size[0] * size[1])
        units = max(1, min(width // 32, height // 18))
        return 32 * units, 18 * units
    source = max(project.sources, key=lambda source: source.width * source.height)
    width, height = source.width, source.height
    if source.rotation % 180 == 90:
        width, height = height, width
    return width + width % 2, height + height % 2


def normalize_timing(
    source: Path,
    destination: Path,
    fps: int,
    cancel: Event | None = None,
    screen_guard: bool = False,
    bookend_plan: RenderPlan | None = None,
    square_bands: bool = False,
    encoder: str = "auto",
) -> list[int]:
    """Rebuild the frame clock, optionally replacing brief menu flashes.

    MP4 segment durations are rounded by the concat demuxer; thousands of short
    segments accumulate that rounding. Retiming is independent of those timestamps.
    """
    rejected: list[int] = []
    filters = f"settb=1/{fps},setpts=N"
    if screen_guard:
        from sculptcut.frame_guard import flash_filter

        filters, rejected = flash_filter(source, fps, cancel)
    # Chunk encodes can alternate between full and limited color range. Keep one
    # filter instance alive across those changes so setpts=N does not restart at
    # a chunk boundary and send timestamps back to zero.
    filters = f"format=yuv420p,{filters}"
    if bookend_plan is not None and (tail := final_frames(bookend_plan)):
        info = probe(source)
        filters += "," + bookend_filter(
            bookend_plan.actual_frame_count, tail, fps, info.width, info.height
        )
    if square_bands:
        # Restore flat bands after the low-cost MPEG-4 preview intermediate.
        filters += (
            ",drawbox=x=0:y=0:w=iw:h=(ih-iw)/2:color=0x181818:t=fill,"
            "drawbox=x=0:y=(ih+iw)/2:w=iw:h=(ih-iw)/2:color=0x181818:t=fill"
        )
    with tempfile.TemporaryDirectory(prefix="clock-", dir=destination.parent) as scratch:
        script = Path(scratch) / "filter.txt"
        script.write_text(filters, encoding="utf-8")
        automatic = encoder == "auto"
        if automatic:
            encoder = "h264_nvenc" if doctor().get("h264_nvenc") else "libx264"
        try:
            _normalize(source, destination, fps, script, cancel, encoder)
        except Cancelled:
            raise
        except MediaError:
            if encoder == "libx264" or not automatic:
                raise
            logging.getLogger("artlapse").warning("Final NVENC failed; retrying with CPU")
            destination.unlink(missing_ok=True)
            _normalize(source, destination, fps, script, cancel, "libx264")
    return rejected


def _normalize(
    source: Path,
    destination: Path,
    fps: int,
    script: Path,
    cancel: Event | None,
    encoder: str = "libx264",
) -> None:
    logging.getLogger("artlapse").info("Final video encoder: %s", encoder)
    quality = (
        ["-preset", "p4", "-rc", "vbr", "-cq", str(FINAL_CRF), "-b:v", "0"]
        if encoder == "h264_nvenc"
        else ["-preset", "fast", "-crf", str(FINAL_CRF)]
    )
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-nostdin",
            "-xerror",
            "-reinit_filter",
            "0",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-an",
            "-filter_script:v",
            str(script),
            "-fps_mode",
            "passthrough",
            "-enc_time_base",
            f"1:{fps}",
            "-c:v",
            encoder,
            *quality,
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(destination),
        ],
        cancel,
    )


def verify_output(
    path: Path, frames: int, fps: int, cancel: Event | None = None, expected_audio: bool = False
) -> None:
    metadata_text = run_process([
        discover("ffprobe"), "-v", "error", "-count_frames", "-show_streams",
        "-show_format", "-of", "json", str(path),
    ], cancel)
    info = parse_probe(metadata_text, path)
    if (
        info.duration_us is None
        or abs(info.duration_us - frames * 1_000_000 / fps) > 1_000_000 / fps
    ):
        raise MediaError(
            f"Rendered duration differs from plan: actual={info.duration_us}us, "
            f"expected={frames * 1_000_000 / fps:.0f}us"
        )
    if bool(info.audio_streams) != expected_audio:
        raise MediaError("Rendered audio stream does not match settings")
    metadata = json.loads(metadata_text)
    stream = next(row for row in metadata["streams"] if row["index"] == info.stream_index)
    try:
        counted = int(stream["nb_read_frames"])
    except (KeyError, ValueError, TypeError) as exc:
        raise MediaError("Cannot count rendered video frames") from exc
    if counted != frames:
        raise MediaError("Rendered frame count differs from plan")
    run_process(
        [discover("ffmpeg"), "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"], cancel
    )


def chunk_decisions(plan: RenderPlan) -> list[RenderDecision]:
    result = []
    for decision in plan.decisions:
        if decision.action == "DROP":
            continue
        if decision.action == "MONTAGE":
            samples = decision.sample_times_us
            for index, timestamp in enumerate(samples):
                result.append(
                    decision.model_copy(
                        update={
                            "action": "HOLD",
                            "source_start_us": timestamp,
                            "source_end_us": decision.source_end_us,
                            "output_frame_count": decision.output_frame_count
                            * (index + 1)
                            // len(samples)
                            - decision.output_frame_count * index // len(samples),
                        }
                    )
                )
            continue
        duration = decision.source_end_us - decision.source_start_us
        count = decision.output_frame_count
        chunks = min(
            count,
            max(1, math.ceil(duration / 300_000_000), math.ceil(count / (30 * plan.output_fps))),
        )
        if decision.action == "HOLD":
            chunks = 1
        for index in range(chunks):
            first_frame = count * index // chunks
            last_frame = count * (index + 1) // chunks
            # Match the tracking decoder's output-frame sampling grid exactly.
            # Equal source-time chunks drift when their frame counts are rounded.
            start = decision.source_start_us + duration * first_frame // count
            end = decision.source_start_us + duration * last_frame // count
            result.append(
                decision.model_copy(
                    update={
                        "source_start_us": start,
                        "source_end_us": end,
                        "output_frame_count": last_frame - first_frame,
                    }
                )
            )
    return result


@screen_check_cache()
def render(
    root: Path,
    project: Project,
    plan: RenderPlan,
    destination: Path,
    cancel: Event | None = None,
    preview: bool = False,
    encoder: str = "auto",
    progress: Callable[[float], None] | None = None,
    encoder_status: Callable[[str], None] | None = None,
    variant_project: Project | None = None,
    variant_parent: Path | None = None,
    variant_operation: str = "render",
    variant_cuts: list[tuple[int, int]] | None = None,
    variant_episode_base: RenderPlan | None = None,
) -> Path:
    if preview:
        from sculptcut.preview import render_preview

        if encoder_status:
            encoder_status("CPU · быстрый предпросмотр")
        return render_preview(root, project, plan, destination, cancel, progress)
    if encoder not in ("auto", "libx264", "h264_nvenc"):
        raise ValueError("Unknown encoder")
    selected = "h264_nvenc" if encoder != "libx264" and doctor().get("h264_nvenc") else "libx264"
    if encoder_status:
        encoder_status("NVIDIA NVENC · фильтры CPU" if selected == "h264_nvenc" else "CPU")
    try:
        return _render(
            root,
            project,
            plan,
            destination,
            cancel,
            preview,
            selected,
            progress,
            variant_project,
            variant_parent,
            variant_operation,
            variant_cuts,
            variant_episode_base,
        )
    except Cancelled:
        raise
    except MediaError:
        if selected == "libx264":
            raise
        # Retry all chunks with one codec configuration after a hardware failure.
        if encoder_status:
            encoder_status("CPU · возврат после ошибки GPU")
        return _render(
            root,
            project,
            plan,
            destination,
            cancel,
            preview,
            "libx264",
            progress,
            variant_project,
            variant_parent,
            variant_operation,
            variant_cuts,
            variant_episode_base,
        )


def _render(
    root: Path,
    project: Project,
    plan: RenderPlan,
    destination: Path,
    cancel: Event | None,
    preview: bool,
    encoder: str,
    progress: Callable[[float], None] | None,
    variant_project: Project | None,
    variant_parent: Path | None,
    variant_operation: str,
    variant_cuts: list[tuple[int, int]] | None,
    variant_episode_base: RenderPlan | None,
) -> Path:
    validate_sources(root, project)
    validate_plan(project, plan)
    output_frames = output_frame_count(project.settings, plan.output_fps, plan.actual_frame_count)
    music = soundtrack(root, project.settings, output_frames / plan.output_fps, cancel)
    if music:
        validate_music(music)
    destination = destination.resolve()
    if destination.suffix.lower() != ".mp4" or destination.exists():
        raise ValueError("Output must be a new .mp4 path")
    if any(destination == s.path.resolve() for s in project.sources):
        raise ValueError("Output cannot overwrite a source")
    destination.parent.mkdir(parents=True, exist_ok=True)
    sources = {s.id: s for s in project.sources}
    fps = plan.output_fps
    width, height = export_size(project, plan)
    square = uses_square(project.settings)
    finale = next(d for d in reversed(plan.decisions) if d.action != "DROP")
    with nullcontext(render_job(destination, plan, encoder, preview)) as directory:
        temporary = Path(directory)
        names = []
        completed = 0
        batches = coalesce_chunks(chunk_decisions(plan), sources, fps)
        for index, decision in enumerate(batches):
            name = f"chunk-{index:06d}.mp4"
            if reusable_chunk(temporary, name):
                names.append(name)
                completed += decision.output_frame_count
                if progress:
                    progress(completed / plan.actual_frame_count * 0.85)
                continue
            (temporary / name).unlink(missing_ok=True)
            source = sources[decision.source_id]
            media_path = proxy_path(root, project, source) if preview else source.path
            input_args = [
                "-ss", f"{decision.source_start_us / 1e6:.6f}",
                "-reinit_filter", "0", "-i", str(media_path),
            ]
            stream_map = "0:v:0" if preview else f"0:{source.stream_index}"
            if decision.action == "HOLD":
                timestamp = decision.source_start_us
                if decision.provenance in ("user:final_tail", "user:shorts_final"):
                    timestamp = max(timestamp, decision.source_end_us - math.ceil(
                        1_000_000*source.fps_den/source.fps_num))
                media_path = still_frame(
                    source, timestamp, temporary / "still.png", cancel,
                )
                input_args = ["-loop", "1", "-framerate", str(fps), "-i", str(media_path)]
                stream_map = "0:v:0"
            duration = (decision.source_end_us - decision.source_start_us) / 1_000_000
            count = decision.output_frame_count
            ratio = count / fps / duration
            padding = count / fps
            still_motion = ""
            if decision.action == "HOLD":
                duration, ratio, padding = 1 / fps, 1, count / fps
                zoom_max = float(plan.settings.get("quiet_hold_zoom_max", 1.0))
                quiet_min = float(plan.settings.get("quiet_hold_zoom_min_seconds", 0.7))
                if (
                    project.settings.preset == "beat_shorts"
                    and count / fps >= quiet_min
                    and count > 1
                    and zoom_max > 1
                    and not (final_frames(plan) and decision.source_id == finale.source_id
                             and decision.source_start_us == finale.source_start_us)
                ):
                    increment = (zoom_max - 1) / (count - 1)
                    still_motion = (
                        f"loop=loop={count - 1}:size=1:start=0,"
                        f"zoompan=z='min(zoom+{increment:.10f},{zoom_max:.6f})':"
                        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                        f"d=1:s={width}x{width if square else height}:fps={fps},"
                    )
            codec_options = (
                ["-preset", "p4", "-cq", str(INTERMEDIATE_QUALITY)]
                if encoder == "h264_nvenc"
                else ["-preset", "fast", "-crf", str(INTERMEDIATE_QUALITY)]
            )
            crop_filter = ""
            if isinstance(decision.crop, Roi):
                crop = decision.crop
                crop_filter = (
                    f"crop=w='max(2,trunc(iw*{crop.width:.9f}/2)*2)':"
                    f"h='max(2,trunc(ih*{crop.height:.9f}/2)*2)':"
                    f"x='min(iw-ow,trunc(iw*{crop.x:.9f}/2)*2)':"
                    f"y='min(ih-oh,trunc(ih*{crop.y:.9f}/2)*2)',"
                )
            name = f"chunk-{index:06d}.mp4"
            composition = (
                f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
                f"force_divisible_by=2:flags=lanczos,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
            )
            if project.settings.preset in SHORTS_PRESETS:
                composition = (
                    f"split[back][front];[back]scale={width}:{height}:"
                    f"force_original_aspect_ratio=increase,crop={width}:{height},"
                    "boxblur=30:2[blur];[front]"
                    f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
                    "force_divisible_by=2:flags=lanczos[sharp];"
                    "[blur][sharp]overlay=(W-w)/2:(H-h)/2,"
                )
            if decision.subject_camera:
                sw, sh = source.width, source.height
                if source.rotation % 180 == 90:
                    sw, sh = sh, sw
                crop_filter = ""
                composition = camera_filter(
                    decision.subject_camera, decision.source_start_us, ratio, sw, sh,
                    width, width if square else height
                )
            if square:
                if not isinstance(decision.crop, Roi):
                    raise ValueError("Square Shorts requires a planned source crop")
                if decision.subject_camera is None:
                    composition = f"scale={width}:{width}:flags=lanczos,"
                still_motion += square_composition(width, height)
            filter_path = temporary / "frame-filter.txt"
            timing_filter = (
                f"trim=end_frame=1,loop=loop=-1:size=1:start=0,setpts=N/{fps}/TB,"
                if decision.action == "HOLD" else
                f"setpts=PTS-STARTPTS,trim=duration={duration:.6f},setpts=PTS*{ratio:.12f},"
            )
            filter_path.write_text(
                timing_filter +
                f"fps={fps},{crop_filter}{composition}setsar=1,{still_motion}"
                f"tpad=stop_mode=clone:stop_duration={padding:.8f}",
                encoding="utf-8",
            )
            run_process(
                [
                    discover("ffmpeg"),
                    "-v",
                    "error",
                    "-nostdin",
                    "-xerror",
                    *input_args,
                    "-map",
                    stream_map,
                    "-an",
                    "-filter_script:v",
                    str(filter_path),
                    "-frames:v",
                    str(count),
                    "-c:v",
                    encoder,
                    *codec_options,
                    "-pix_fmt",
                    "yuv420p",
                    str(temporary / name),
                ],
                cancel,
            )
            verify_output(temporary / name, count, fps, cancel)
            record_chunk(temporary, name)
            names.append(name)
            completed += count
            if progress:
                progress(completed / plan.actual_frame_count * 0.85)
        listing = temporary / "concat.txt"
        listing.write_text("".join(f"file '{name}'\n" for name in names), encoding="utf-8")
        combined = temporary / "combined.mp4"
        combined.unlink(missing_ok=True)
        run_process(
            [
                discover("ffmpeg"),
                "-v",
                "error",
                "-nostdin",
                "-f",
                "concat",
                "-safe",
                "1",
                "-i",
                str(listing),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(combined),
            ],
            cancel,
        )
        normalized = temporary / "normalized.mp4"
        normalized.unlink(missing_ok=True)
        guarded_frames = normalize_timing(
            combined,
            normalized,
            fps,
            cancel,
            screen_guard=plan.settings.get("story") == "process",
            bookend_plan=plan,
            square_bands=square,
            encoder=encoder,
        )
        combined = normalized
        if progress:
            progress(0.95)
        verify_output(combined, plan.actual_frame_count, fps, cancel)
        if (image := active_intro_image(project.settings)) is not None:
            intro = temporary / "intro.mp4"
            intro.unlink(missing_ok=True)
            prepend_intro(
                combined,
                image,
                intro,
                width=width,
                height=height,
                fps=fps,
                body_frames=plan.actual_frame_count,
                settings=project.settings,
                encoder=encoder,
                cancel=cancel,
            )
            verify_output(intro, output_frames - presentation_frames(project.settings, fps),
                          fps, cancel)
            combined = intro
        if project.settings.presentation_video is not None:
            presentation = temporary / "presentation.mp4"
            presentation.unlink(missing_ok=True)
            attach_presentation(combined, presentation, project.settings, width=width,
                                height=height, fps=fps, encoder=encoder, cancel=cancel)
            verify_output(presentation, output_frames, fps, cancel)
            combined = presentation
        if music:
            with_audio = temporary / "with-music.mp4"
            with_audio.unlink(missing_ok=True)
            add_music(
                combined,
                music,
                with_audio,
                output_frames / fps,
                project.settings.music_loop,
                project.settings.music_fade_seconds,
                cancel,
            )
            verify_output(with_audio, output_frames, fps, cancel, expected_audio=True)
            combined = with_audio
        validate_sources(root, project)
        validate_plan(project, plan)
        if destination.exists():
            raise ValueError("Output appeared during render; refusing overwrite")
        os.rename(combined, destination)
        from sculptcut.variants import save_variant

        save_variant(
            destination,
            project=variant_project or project,
            plan=plan,
            music=music,
            parent=variant_parent,
            cuts=variant_cuts,
            operation=variant_operation,
            episode_base_plan=variant_episode_base,
        )
        if guarded_frames:
            atomic_write(
                metadata_path(destination, ".guard.json"),
                json.dumps(
                    {
                        "policy": "replace_screen_flashes_with_clean_neighbours",
                        "frames": guarded_frames,
                    }
                ),
            )
        finish_job(temporary)
        if progress:
            progress(1)
    return destination
