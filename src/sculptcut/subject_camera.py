"""Shared subject-centred camera for original export and proxy preview."""

from bisect import bisect_right
from pathlib import Path
from threading import Event

import cv2
import numpy as np
from numpy.typing import NDArray

from sculptcut.domain import SourceMedia, SubjectCamera
from sculptcut.media import MediaError, discover, run_process
from sculptcut.roi import pixel_bounds


def still_frame(
    source: SourceMedia, time_us: int, destination: Path,
    cancel: Event | None = None, sample_width: int | None = None,
) -> Path:
    """Decode one real frame; container/audio tails may extend beyond video EOF."""
    scale = f"scale='min({sample_width},iw)':-2," if sample_width else ""
    destination.unlink(missing_ok=True)
    run_process([
        discover("ffmpeg"), "-v", "error", "-nostdin", "-y",
        "-ss", f"{time_us / 1e6:.6f}", "-i", str(source.path),
        "-map", f"0:{source.stream_index}", "-an", "-frames:v", "1",
        "-vf", scale + "null", str(destination),
    ], cancel)
    if destination.is_file() and destination.stat().st_size:
        return destination
    # Restrict fallback to a small tail window; never scan or reverse a whole
    # long source. Scale first for analysis/preview to bound frame memory.
    lookback = max(0.25, 2 * source.fps_den / max(1, source.fps_num))
    start = max(0.0, time_us / 1e6 - lookback)
    run_process([
        discover("ffmpeg"), "-v", "error", "-nostdin", "-y",
        "-ss", f"{start:.6f}", "-i", str(source.path),
        "-map", f"0:{source.stream_index}", "-an", "-t", f"{lookback:.6f}",
        "-frames:v", "1", "-vf",
        scale + f"trim=duration={lookback:.6f},reverse,trim=end_frame=1",
        str(destination),
    ], cancel)
    if not destination.is_file() or not destination.stat().st_size:
        raise MediaError(f"Cannot decode still frame at {time_us / 1e6:.3f}s: {source.path}")
    return destination


def tracking_clip(
    source: SourceMedia,
    start_us: int,
    end_us: int,
    destination: Path,
    cancel: Event | None = None,
    output_frames: int | None = None,
    output_fps: int = 30,
) -> Path:
    """Use the export decoder and timeline, never OpenCV's long-file seek."""
    ratio = output_frames / output_fps / ((end_us - start_us) / 1e6) if output_frames else 1.0
    filters = (
        f"trim=duration={(end_us - start_us) / 1e6:.6f},setpts=(PTS-STARTPTS)*{ratio:.12f},"
        f"fps={output_fps},scale='min(960,iw)':-2"
    )
    if output_frames:
        # Fractional seeks and the last source timestamp may leave one fewer frame.
        # Match the export's bounded tail padding and its exact integer frame budget.
        filters += (
            f",tpad=stop_mode=clone:stop_duration={output_frames / output_fps:.8f},"
            f"trim=end_frame={output_frames}"
        )
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-ss",
            f"{start_us / 1e6:.6f}",
            "-i",
            str(source.path),
            "-map",
            f"0:{source.stream_index}",
            "-an",
            "-vf",
            filters,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "14",
            str(destination),
        ],
        cancel,
    )
    return destination


def center_at(camera: SubjectCamera, time_us: int) -> tuple[float, float]:
    points = camera.points
    index = bisect_right([p.time_us for p in points], time_us)
    if index == 0:
        return points[0].x, points[0].y
    if index == len(points):
        return points[-1].x, points[-1].y
    a, b = points[index - 1], points[index]
    weight = (time_us - a.time_us) / (b.time_us - a.time_us)
    return a.x + (b.x - a.x) * weight, a.y + (b.y - a.y) * weight


def size_at(camera: SubjectCamera, time_us: int) -> tuple[float, float]:
    points = camera.points
    index = bisect_right([p.time_us for p in points], time_us)
    a, b = points[max(0, index - 1)], points[min(index, len(points) - 1)]
    weight = (time_us - a.time_us) / (b.time_us - a.time_us) if b.time_us > a.time_us else 0
    aw, ah = a.width or camera.width, a.height or camera.height
    bw, bh = b.width or camera.width, b.height or camera.height
    return aw + (bw - aw) * weight, ah + (bh - ah) * weight


def camera_preview(
    frame: NDArray[np.uint8], camera: SubjectCamera, time_us: int, width: int, height: int
) -> NDArray[np.uint8]:
    h, w = frame.shape[:2]
    x, y, right, bottom = pixel_bounds(camera.viewport, w, h)
    cx, cy = center_at(camera, time_us)
    camera_w, camera_h = size_at(camera, time_us)
    scale = (
        min(width / (camera_w * w), height / (camera_h * h))
        if camera.fit_subject
        else max(width / (camera.width * w), width / (right - x), height / (bottom - y))
    )
    offset_x = width / 2 - (cx * w - x) * scale
    offset_y = height / 2 - (cy * h - y) * scale
    if camera.blur_background:
        viewport = frame[y:bottom, x:right]
        cover = max(width / (right - x), height / (bottom - y))
        background = cv2.resize(
            viewport,
            (max(width, round((right - x) * cover)), max(height, round((bottom - y) * cover))),
        )
        bx = (background.shape[1] - width) // 2
        by = (background.shape[0] - height) // 2
        background = cv2.GaussianBlur(background[by : by + height, bx : bx + width], (61, 61), 0)
        gray = cv2.cvtColor(background, cv2.COLOR_BGR2GRAY)
        background = cv2.addWeighted(
            background, 0.25, cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR), 0.75, 0
        )
        foreground = cv2.warpAffine(
            viewport,
            np.array([[scale, 0, offset_x], [0, scale, offset_y]], dtype=np.float64),
            (width, height),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(0, 0, 0),
        )
        mask = cv2.warpAffine(
            np.full(viewport.shape[:2], 255, dtype=np.uint8),
            np.array([[scale, 0, offset_x], [0, scale, offset_y]], dtype=np.float64),
            (width, height),
        )
        background[mask > 0] = foreground[mask > 0]
        return background.astype(np.uint8)
    transform = np.array(
        [
            [scale, 0, offset_x],
            [0, scale, offset_y],
        ],
        dtype=np.float64,
    )
    return cv2.warpAffine(
        frame[y:bottom, x:right],
        transform,
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(camera.background_rgb[::-1] if camera.background_rgb
                     else (camera.background,) * 3),
    ).astype(np.uint8)


def camera_filter(
    camera: SubjectCamera,
    start_us: int,
    ratio: float,
    source_width: int,
    source_height: int,
    width: int,
    height: int,
) -> str:
    """Centre the subject in the viewport, with optional fitted blur backing."""
    x, y, r, b = pixel_bounds(camera.viewport, source_width, source_height)
    red, green, blue = camera.background_rgb or (camera.background,) * 3
    background_filter = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},boxblur=30:2,hue=s=0.25"
        if camera.blur_background
        else f"scale={width}:{height},lutrgb=r={red}:g={green}:b={blue}"
    )
    scale = (
        min(width / (camera.width * source_width), height / (camera.height * source_height))
        if camera.fit_subject
        else max(width / (camera.width * source_width), width / (r - x), height / (b - y))
    )
    scaled_w = max(2 if camera.fit_subject else width, round((r - x) * scale / 2) * 2)
    scaled_h = max(2 if camera.fit_subject else height, round((b - y) * scale / 2) * 2)

    def expression(axis: str) -> str:
        # FFmpeg also parses a flat sum recursively. Balance the ramp sum so
        # hundreds of tracking points do not exhaust its expression depth.
        points = camera.points

        def value(index: int) -> float:
            point_value: float | None = getattr(points[index], axis)
            return float(getattr(camera, axis)) if point_value is None else point_value

        terms = [f"{value(0):.9f}"]
        for index, (a, z) in enumerate(zip(points, points[1:], strict=False)):
            delta = value(index + 1) - value(index)
            if abs(delta) < 1e-9:
                continue
            start = (a.time_us - start_us) / 1e6 * ratio
            duration = (z.time_us - a.time_us) / 1e6 * ratio
            terms.append(f"({delta:.9f})*clip((t-({start:.9f}))/{duration:.9f},0,1)")
        while len(terms) > 1:
            terms = [
                f"({terms[i]}+{terms[i + 1]})" if i + 1 < len(terms) else terms[i]
                for i in range(0, len(terms), 2)
            ]
        return terms[0]

    if camera.fit_subject and any(
        p.width is not None or p.height is not None for p in camera.points
    ):
        zoom = (
            f"min({width}/(({expression('width')})*{source_width}),"
            f"{height}/(({expression('height')})*{source_height}))"
        )
        xpos = f"{width}/2-(({expression('x')})*{source_width}-{x})*overlay_w/{r - x}"
        ypos = f"{height}/2-(({expression('y')})*{source_height}-{y})*overlay_h/{b - y}"
        return (
            f"crop={r - x}:{b - y}:{x}:{y}:exact=1,split[seed][subject];"
            f"[seed]{background_filter}[background];"
            f"[subject]scale=w='max(2,trunc(iw*({zoom})/2)*2)':"
            f"h='max(2,trunc(ih*({zoom})/2)*2)':eval=frame:flags=lanczos[tracked];"
            f"[background][tracked]overlay=x='{xpos}':y='{ypos}':eval=frame:format=auto,"
        )

    xpos = f"{width}/2-(({expression('x')})*{source_width}-{x})*{scaled_w}/{r - x}"
    ypos = f"{height}/2-(({expression('y')})*{source_height}-{y})*{scaled_h}/{b - y}"
    color = camera.background
    if camera.fit_subject:
        return (
            f"crop={r - x}:{b - y}:{x}:{y}:exact=1,split[seed][subject];"
            f"[seed]{background_filter}[background];"
            f"[subject]scale={scaled_w}:{scaled_h}:flags=lanczos[tracked];"
            f"[background][tracked]overlay=x='{xpos}':y='{ypos}':eval=frame:format=auto,"
        )
    return (
        f"crop={r - x}:{b - y}:{x}:{y}:exact=1,scale={scaled_w}:{scaled_h}:flags=lanczos,"
        f"split[subject][seed];[seed]scale={width}:{height},"
        f"lutrgb=r={color}:g={color}:b={color}[background];"
        f"[background][subject]overlay=x='{xpos}':y='{ypos}':eval=frame:format=auto,"
    )
