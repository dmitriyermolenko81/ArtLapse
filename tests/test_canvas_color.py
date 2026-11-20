"""Colour and quiet midtones survive the complete accumulated-canvas export."""

import json

import cv2
import numpy as np
import pytest

from sculptcut.domain import CameraPoint, ProjectSettings, Roi, SubjectCamera
from sculptcut.media import discover, doctor, run_process
from sculptcut.planning import build_plan
from sculptcut.project import create_project, save_project
from sculptcut.rendering import render, verify_output
from sculptcut.subject_camera import read_still, tracking_clip


@pytest.mark.parametrize("matrix,color_range,cover,encoder", [
    ("bt709", "tv", False, "libx264"),
    ("bt709", "pc", True, "libx264"),
    ("bt601", "tv", False, "libx264"),
    ("bt601", "pc", False, "libx264"),
    ("bt709", "tv", False, "h264_nvenc"),
])
def test_canvas_preserves_colour_and_flat_midtones(
    tmp_path, monkeypatch, matrix, color_range, cover, encoder,
):
    import sculptcut.rendering as rendering

    if encoder == "h264_nvenc" and not doctor().get("h264_nvenc"):
        pytest.skip("NVENC unavailable")
    image = np.empty((144, 256, 3), np.uint8)
    colours = [(24, 64, 128), (190, 60, 30), (70, 110, 160), (80, 80, 80)]
    for i, colour in enumerate(colours):
        image[:, i*64:(i+1)*64] = colour
    image[80:] = np.linspace(48, 96, 256).astype(np.uint8)[None, :, None]
    png = tmp_path / "образец.png"
    ok, encoded = cv2.imencode('.png', image)
    assert ok
    png.write_bytes(encoded.tobytes())
    source = tmp_path / "source.mp4"
    run_process([
        discover("ffmpeg"), "-v", "error", "-loop", "1", "-i", str(png),
        "-t", "1", "-r", "10", "-vf",
        f"scale=out_color_matrix={matrix}:out_range={color_range}",
        "-c:v", "libx264", "-crf", "0", "-pix_fmt", "yuv444p",
        "-color_range", color_range, "-colorspace",
        "bt709" if matrix == "bt709" else "bt470bg", str(source),
    ])
    root = tmp_path / "project"
    project = create_project(root, [source]).model_copy(update={"settings": ProjectSettings(
        preset="photoshop_shorts", photoshop_keep_model=True, output_fps=10,
        final_view_seconds=0, intro_image=png if cover else None,
        intro_seconds=1, intro_transition_seconds=.2,
    )})
    save_project(root, project)
    plan = build_plan(project, [], 1)
    camera = SubjectCamera(
        viewport=Roi(x=0, y=0, width=1, height=1), fit_subject=True,
        canvas_registered=True, width=1, height=(256/144)*(16/9), background=210,
        points=[CameraPoint(time_us=0, x=.5, y=.5, reference_matched=True)],
    )
    plan = plan.model_copy(update={"decisions": [
        plan.decisions[0].model_copy(update={"subject_camera": camera})]})
    directories = []
    original_record = rendering.record_chunk

    def record(directory, name):
        original_record(directory, name)
        directories.append(directory)
        chunk = directory / name
        metadata = json.loads(run_process([
            discover("ffprobe"), "-v", "error", "-show_streams", "-of", "json", str(chunk),
        ]))["streams"][0]
        assert metadata["codec_name"] == "ffv1" and metadata["pix_fmt"] == "bgr0"
        run_process([discover("ffmpeg"), "-v", "error", "-i", str(chunk),
                     "-frames:v", "1", str(tmp_path / "chunk.png")])
        reader = cv2.VideoCapture(str(chunk))
        ok, frame = reader.read()
        reader.release()
        assert ok and np.array_equal(frame, read_still(tmp_path / "chunk.png"))

    monkeypatch.setattr(rendering, "record_chunk", record)
    output = render(root, project, plan, tmp_path / "output.mp4", encoder=encoder)
    verify_output(output, 18 if cover else 10, 10)
    assert directories and not directories[0].exists()  # RGB checkpoints must not leak
    run_process([discover("ffmpeg"), "-v", "error", "-ss", "1.3" if cover else "0.5",
                 "-i", str(output), "-frames:v", "1", str(tmp_path / "result.png")])
    result = read_still(tmp_path / "result.png")
    run_process([discover("ffmpeg"), "-v", "error", "-i", str(source),
                 "-frames:v", "1", str(tmp_path / "source.png")])
    reference = read_still(tmp_path / "source.png")
    tracking = tracking_clip(project.sources[0], 0, 1_000_000, tmp_path / "tracking.mp4",
                             output_frames=10, output_fps=10, rgb=True)
    reader = cv2.VideoCapture(str(tracking))
    ok, analysis_frame = reader.read()
    reader.release()
    assert ok and np.max(np.abs(analysis_frame.astype(int)-reference.astype(int))) <= 1
    for i in range(4):
        pixels = result[730:830, round((i*64+16)*1080/256):round((i*64+48)*1080/256)]
        expected = reference[16:56, i*64+16:i*64+48].mean((0, 1))
        assert max(np.abs(pixels.mean((0, 1))-expected)) <= 3
        assert max(pixels.std((0, 1))) < 1.1  # no introduced texture in a flat patch
