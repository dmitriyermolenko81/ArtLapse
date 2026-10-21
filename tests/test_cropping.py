from pathlib import Path

import numpy as np

from sculptcut.cropping import apply_crop, object_bounds, union_bounds
from sculptcut.domain import Project, ProjectSettings, Roi
from sculptcut.media import discover, probe, run_process
from sculptcut.planning import build_plan
from sculptcut.project import create_project
from sculptcut.rendering import export_size, render, verify_output


def test_bounds_union_cursor_and_uncertain_fallback() -> None:
    frame = np.zeros((200, 300), dtype=np.uint8)
    assert object_bounds(frame) is None
    frame[40:42, 20:22] = 200
    assert object_bounds(frame) is None
    frame[60:140, 100:200] = 150
    first = object_bounds(frame)
    assert first is not None
    frame[50:150, 80:220] = 150
    zoom = object_bounds(frame)
    assert zoom is not None
    union = union_bounds([first, zoom])
    assert union is not None
    assert union.x <= first.x and union.width >= first.width
    frame[:, 0:30] = 200
    assert object_bounds(frame) is None


def test_canvas_real_render(tmp_path: Path) -> None:
    source = tmp_path / "video.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=160x120:r=15:d=2",
            "-c:v",
            "libx264",
            str(source),
        ]
    )
    root = tmp_path / "project"
    project = create_project(root, [source])
    plan = build_plan(project, [], 1)
    cropped = plan.decisions[0].model_copy(
        update={"crop": Roi(x=0.2, y=0.2, width=0.6, height=0.6)}
    )
    plan = plan.model_copy(update={"decisions": [cropped]})
    output = render(root, project, plan, tmp_path / "canvas.mp4", encoder="libx264")
    verify_output(output, 30, 30)


def test_timelapse_viewport_exports_cropped_resolution(tmp_path: Path) -> None:
    source = tmp_path / "video.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=160x120:r=15:d=2",
            "-c:v",
            "libx264",
            str(source),
        ]
    )
    root = tmp_path / "project"
    created = create_project(root, [source])
    approximate = created.model_copy(
        update={"settings": ProjectSettings(preset="timelapse", crop_mode="viewport")}
    )
    approximate_plan = apply_crop(root, approximate, build_plan(approximate, [], 1), None)
    approximate_crop = approximate_plan.decisions[0].crop
    assert isinstance(approximate_crop, Roi)
    assert approximate_crop.width * 160 / (approximate_crop.height * 120) == 16 / 9
    project = Project.model_validate(
        created.model_dump()
        | {
            "settings": ProjectSettings(
                preset="timelapse",
                crop_mode="viewport",
                timelapse_viewport_roi=Roi(x=0.25, y=0.25, width=0.5, height=0.5),
            ).model_dump(),
        }
    )
    plan = apply_crop(root, project, build_plan(project, [], 1), None)
    assert export_size(project, plan) == (64, 36)
    output = render(root, project, plan, tmp_path / "viewport-output.mp4", encoder="libx264")
    verify_output(output, 30, 30)
    assert (probe(output).width, probe(output).height) == (64, 36)
