"""Decoded color fidelity across JPEG intros, stills and encoder boundaries."""

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from sculptcut.domain import ProjectSettings
from sculptcut.media import discover, doctor, run_process
from sculptcut.planning import build_plan
from sculptcut.project import create_project
from sculptcut.rendering import render, verify_output


def decode(path: Path, seconds: float) -> np.ndarray:
    # OpenCV VideoCapture uses its own YUV conversion and can ignore matrix tags.
    # Decode via FFmpeg to RGB before OpenCV reads the lossless reference pixels.
    image = path.with_suffix(f".{round(seconds * 1000)}.png")
    run_process([
        discover("ffmpeg"), "-v", "error", "-ss", str(seconds), "-i", str(path),
        "-frames:v", "1", "-update", "1", "-y", str(image),
    ])
    frame = cv2.imdecode(np.fromfile(image, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert frame is not None
    return frame


@pytest.mark.parametrize("encoder", ["libx264", "h264_nvenc"])
@pytest.mark.parametrize("color_range", ["tv", "pc"])
@pytest.mark.parametrize("cover", ["jpg", "png", None])
def test_jpeg_intro_preserves_source_colors(
    tmp_path: Path, encoder: str, color_range: str, cover: str | None,
) -> None:
    if encoder == "h264_nvenc" and not doctor().get("h264_nvenc"):
        pytest.skip("NVENC unavailable")
    # Neutral shadows/highlights plus skin and saturated UI colors. Sample inside
    # each flat patch so codec ringing and chroma subsampling do not mask shifts.
    colors = [(8, 8, 8), (32, 32, 32), (72, 72, 72), (128, 128, 128),
              (235, 235, 235), (70, 110, 160), (20, 130, 240), (190, 60, 30)]
    image = tmp_path / f"cover.{cover or 'png'}"
    patches = np.zeros((144, 256, 3), dtype=np.uint8)
    for index, color in enumerate(colors):
        patches[:, index * 32:(index + 1) * 32] = color
    ok, data = cv2.imencode(image.suffix, patches, [cv2.IMWRITE_JPEG_QUALITY, 100])
    assert ok
    data.tofile(str(image))
    source = tmp_path / "source.mp4"
    run_process([
        discover("ffmpeg"), "-v", "error", "-loop", "1", "-i", str(image),
        "-t", "2", "-vf", f"scale=out_color_matrix=bt709:out_range={color_range}",
        "-c:v", "libx264", "-crf", "10", "-pix_fmt", "yuv420p",
        "-color_range", color_range, "-colorspace", "bt709", "-color_trc", "bt709",
        "-color_primaries", "bt709", str(source),
    ])
    root = tmp_path / "project"
    project = create_project(root, [source]).model_copy(update={
        "settings": ProjectSettings(preset="speed", intro_image=image if cover else None,
                                    intro_seconds=1, intro_transition_seconds=.25),
    })
    plan = build_plan(project, [], 2)
    first = plan.decisions[0]
    plan = plan.model_copy(update={"decisions": [
        first.model_copy(update={"source_end_us": 1_000_000, "output_frame_count": 30}),
        first.model_copy(update={"action": "HOLD", "source_start_us": 1_000_000,
                                 "output_frame_count": 30, "provenance": "user:still"}),
    ]})
    output = render(root, project, plan, tmp_path / "export.mp4", encoder=encoder)
    verify_output(output, 82 if cover else 60, 30)
    reference = decode(source, .5).astype(float)
    for seconds in ((1.2, 2.2) if cover else (.5, 1.5)):
        exported = decode(output, seconds).astype(float)
        for index in range(len(colors)):
            region = np.s_[32:112, index * 32 + 8:(index + 1) * 32 - 8]
            error = np.abs(reference[region].mean(axis=(0, 1))
                           - exported[region].mean(axis=(0, 1)))
            assert max(error) <= 4, (seconds, index, error)
    streams = json.loads(run_process([
        discover("ffprobe"), "-v", "error", "-show_streams", "-of", "json", str(output),
    ]))["streams"]
    assert streams[0]["color_range"] == "tv"
    assert streams[0]["color_space"] == "bt709"
    assert streams[0]["color_transfer"] == "bt709"
    assert streams[0]["color_primaries"] == "bt709"


@pytest.mark.parametrize("encoder", ["libx264", "h264_nvenc"])
def test_presentation_keeps_its_full_range_colors(tmp_path: Path, encoder: str) -> None:
    from sculptcut.presentation import attach_presentation

    if encoder == "h264_nvenc" and not doctor().get("h264_nvenc"):
        pytest.skip("NVENC unavailable")
    clips = []
    for name, matrix, color_range in (("body", "bt709", "tv"), ("turn", "bt601", "pc")):
        clip = tmp_path / f"{name}.mp4"
        run_process([
            discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
            "color=c=0xa46e46:s=256x144:r=30:d=1", "-vf",
            f"scale=out_color_matrix={matrix}:out_range={color_range}",
            "-c:v", "libx264", "-crf", "10", "-color_range", color_range,
            "-colorspace", "bt709" if matrix == "bt709" else "bt470bg", str(clip),
        ])
        clips.append(clip)
    settings = ProjectSettings(preset="speed", presentation_video=clips[1],
                               presentation_seconds=1)
    output = tmp_path / "presentation.mp4"
    attach_presentation(clips[0], output, settings, width=256, height=144,
                        fps=30, encoder=encoder)
    verify_output(output, 60, 30)
    for seconds, source in ((.5, clips[1]), (1.5, clips[0])):
        reference = decode(source, .5).astype(float).mean(axis=(0, 1))
        exported = decode(output, seconds).astype(float).mean(axis=(0, 1))
        assert max(np.abs(reference - exported)) <= 4
