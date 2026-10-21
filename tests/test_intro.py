from pathlib import Path

import cv2
import numpy as np
import pytest

from sculptcut.domain import ProjectSettings
from sculptcut.intro import output_frame_count
from sculptcut.media import discover, run_process
from sculptcut.planning import build_plan, validate_plan
from sculptcut.project import create_project
from sculptcut.proxy import make_proxies
from sculptcut.rendering import render, verify_output


def make_image(path: Path, color: tuple[int, int, int]) -> None:
    frame = np.full((180, 260, 3), color, dtype=np.uint8)
    encoded, data = cv2.imencode(".png", frame)
    assert encoded
    data.tofile(str(path))


@pytest.mark.parametrize("preset", ["timelapse", "shorts"])
def test_intro_image_crossfades_into_real_export(tmp_path: Path, preset: str) -> None:
    source = tmp_path / "source.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x120:r=30:d=2",
            "-c:v",
            "libx264",
            str(source),
        ]
    )
    image = tmp_path / "модель preview.png"
    make_image(image, (0, 220, 0))
    music = None
    if preset == "timelapse":
        music = tmp_path / "music.wav"
        run_process(
            [
                discover("ffmpeg"),
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=3",
                str(music),
            ]
        )
    root = tmp_path / "project"
    project = create_project(root, [source]).model_copy(
        update={
            "settings": ProjectSettings(
                preset=preset,
                intro_image=image,
                intro_seconds=1,
                intro_transition_seconds=0.25,
                music_path=music,
            )
        }
    )
    plan = build_plan(project, [], 1)
    expected = output_frame_count(project.settings, 30, plan.actual_frame_count)
    assert expected == 52
    output = render(root, project, plan, tmp_path / f"{preset}.mp4", encoder="libx264")
    verify_output(output, expected, 30, expected_audio=music is not None)
    if preset == "timelapse":
        make_proxies(root, project)
        preview = render(root, project, plan, tmp_path / "preview.mp4", preview=True)
        verify_output(preview, expected, 30, expected_audio=True)

    capture = cv2.VideoCapture(str(output))
    try:
        ok, first = capture.read()
        assert ok
        assert first[:, :, 1].mean() > first[:, :, 0].mean() * 4
        assert first[:, :, 1].mean() > first[:, :, 2].mean() * 4
        capture.set(cv2.CAP_PROP_POS_FRAMES, expected - 1)
        ok, last = capture.read()
        assert ok
        assert last[:, :, 0].mean() > last[:, :, 1].mean() * 4
        assert last[:, :, 0].mean() > last[:, :, 2].mean() * 4
    finally:
        capture.release()


def test_intro_is_optional_and_image_changes_invalidate_plan(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:r=10:d=1",
            "-c:v",
            "libx264",
            str(source),
        ]
    )
    plain = create_project(tmp_path / "plain", [source])
    plain_plan = build_plan(plain, [], 0.5)
    assert output_frame_count(plain.settings, 30, plain_plan.actual_frame_count) == 15

    image = tmp_path / "cover.png"
    make_image(image, (0, 200, 0))
    project = plain.model_copy(
        update={"settings": ProjectSettings(intro_image=image)}
    )
    plan = build_plan(project, [], 0.5)
    make_image(image, (0, 0, 200))
    with pytest.raises(ValueError, match="Intro image changed"):
        validate_plan(project, plan)
