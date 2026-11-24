"""Frame-accurate cuts in a constant-frame-rate ArtLapse H.264 export."""

import json
from bisect import bisect_left, bisect_right
from pathlib import Path
from threading import Event

from sculptcut.media import MediaError, MediaInfo, discover, run_process
from sculptcut.remux_check import decode_delay, video_samples


def splice_video(source: Path, folder: Path, ranges: list[tuple[int, int]], info: MediaInfo,
                 cancel: Event) -> Path | None:
    if info.fps_den != 1 or info.fps_num <= 0 or info.start_time != 0:
        return None
    fps = info.fps_num
    data = json.loads(run_process([
        discover("ffprobe"), "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
        "-show_frames", "-show_streams", "-show_entries",
        "stream=codec_name,nb_frames,pix_fmt,r_frame_rate:frame=best_effort_timestamp_time",
        "-of", "json", str(source)], cancel))
    stream = data["streams"][0]
    if (stream.get("codec_name") != "h264" or stream.get("pix_fmt") != "yuv420p"
            or stream.get("r_frame_rate") != f"{fps}/1" or not stream.get("nb_frames")):
        return None
    total = int(stream["nb_frames"])
    keys = sorted({round(float(f["best_effort_timestamp_time"]) * fps)
                   for f in data["frames"]} | {total})
    if keys[0] != 0:
        return None
    spans = []
    for a, b in ranges:
        start = min(total, (a * fps + 500_000) // 1_000_000)
        end = min(total, (b * fps + 500_000) // 1_000_000)
        if start >= end:
            continue
        first = keys[bisect_left(keys, start)]
        last = keys[bisect_right(keys, end) - 1]
        if first >= last:
            spans.append((start, end, False))
        else:
            if start < first:
                spans.append((start, first, False))
            spans.append((first, last, True))
            if last < end:
                spans.append((last, end, False))
    if not any(copy for _, _, copy in spans):
        return None
    result = folder / "splice.mp4"
    ffmpeg = discover("ffmpeg")
    rows = []
    # Hardware and software encoders may use different reorder delays even
    # with the same SPS has_b_frames. Preserve presentation timestamps and
    # give all joined packets a common, monotonic decoding clock.
    try:
        reorder = max(2, decode_delay(source, fps, cancel))
    except ValueError:
        return None
    try:
        for index, (start, end, copy) in enumerate(spans):
            part = folder / f"splice-{index}.mp4"
            codec = ["-c:v", "copy"] if copy else ["-c:v", "libx264", "-preset", "fast",
                                                    "-crf", "18", "-pix_fmt", "yuv420p"]
            run_process([ffmpeg, "-v", "error", "-xerror", "-ss", f"{start / fps:.6f}",
                         "-i", str(source), "-map", "0:v:0", "-an", "-frames:v", str(end-start),
                         *codec, "-video_track_timescale", str(fps * 1000), str(part)], cancel)
            if video_samples(part, cancel) != end - start:
                return None
            rows.append(f"file '{part.name}'\nduration {(end-start)/fps:.9f}\n")
        listing = folder / "splice.txt"
        listing.write_text("".join(rows), encoding="utf-8")
        run_process([ffmpeg, "-v", "error", "-xerror", "-f", "concat", "-safe", "1",
                     "-i", str(listing), "-c:v", "copy",
                     "-bsf:v", f"setts=pts=PTS:dts=(N-{reorder})*DURATION",
                     "-movflags", "+faststart",
                     str(result)], cancel)
        if video_samples(result, cancel) == sum(b-a for a, b, _ in spans):
            return result
    except MediaError:
        if cancel.is_set():
            raise
    return None
