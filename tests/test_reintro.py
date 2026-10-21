from pathlib import Path

import cv2
import numpy as np
import pytest

from sculptcut.domain import ProjectSettings
from sculptcut.intro import output_frame_count
from sculptcut.media import discover, run_process
from sculptcut.planning import build_plan
from sculptcut.project import create_project, load_project, relink_sources, save_project
from sculptcut.reintro import replace_intro
from sculptcut.rendering import render, verify_output
from sculptcut.variants import read_variant


def make_image(path: Path, color: tuple[int, int, int]) -> None:
    frame = np.full((120, 160, 3), color, dtype=np.uint8)
    encoded, data = cv2.imencode(".png", frame)
    assert encoded
    data.tofile(str(path))


def test_replace_intro_reuses_plan_and_preserves_music(tmp_path: Path) -> None:
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
    old_image = tmp_path / "old.png"
    new_image = tmp_path / "new.png"
    make_image(old_image, (0, 220, 0))
    make_image(new_image, (0, 0, 220))
    root = tmp_path / "project"
    project = create_project(root, [source]).model_copy(
        update={
            "settings": ProjectSettings(
                intro_image=old_image,
                intro_seconds=1,
                intro_transition_seconds=0.25,
                music_path=music,
            )
        }
    )
    save_project(root, project)
    plan = build_plan(project, [], 1)
    original = render(root, project, plan, root / "renders" / "original.mp4", encoder="libx264")

    moved = tmp_path / "moved-source.mp4"
    source.rename(moved)
    relink_sources(root, {project.sources[0].id: moved})

    progress: list[float] = []
    result = replace_intro(
        root,
        original,
        new_image,
        None,
        progress.append,
        encoder="libx264",
    )
    expected = output_frame_count(project.settings, 30, plan.actual_frame_count)
    verify_output(result, expected, 30, expected_audio=True)
    assert progress[-1] == 1
    assert original.is_file()
    assert result.name == "intro-variant-001_timelapse.mp4"
    state = read_variant(result)
    assert state["operation"] == "intro"
    assert state["parent"] == original.name
    assert state["music"]
    assert load_project(root).settings.intro_image == new_image

    capture = cv2.VideoCapture(str(result))
    try:
        ok, first = capture.read()
        assert ok and first[:, :, 2].mean() > first[:, :, 0].mean() * 4
        capture.set(cv2.CAP_PROP_POS_FRAMES, expected - 1)
        ok, last = capture.read()
        assert ok and last[:, :, 0].mean() > last[:, :, 2].mean() * 4
    finally:
        capture.release()


@pytest.mark.parametrize("had_intro", [True, False])
def test_trim_music_intro_chain_keeps_cuts(tmp_path: Path, had_intro: bool) -> None:
    from threading import Event

    from sculptcut.output_cuts import cut_output
    from sculptcut.remusic import replace_music
    from sculptcut.variants import cut_history

    source = tmp_path / "source.mp4"
    run_process([
        discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
        "testsrc2=s=160x120:r=30:d=3", "-c:v", "libx264", str(source),
    ])
    folder = tmp_path / "music"
    folder.mkdir()
    run_process([
        discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
        "sine=frequency=440:duration=5", str(folder / "track.wav"),
    ])
    old_image, new_image = tmp_path / "old.png", tmp_path / "new.png"
    make_image(old_image, (0, 220, 0))
    make_image(new_image, (0, 0, 220))
    root = tmp_path / "project"
    project = create_project(root, [source]).model_copy(update={"settings": ProjectSettings(
        intro_image=old_image if had_intro else None,
        intro_seconds=1, intro_transition_seconds=0.25,
    )})
    save_project(root, project)
    plan = build_plan(project, [], 2)
    original = render(root, project, plan, root / "renders" / "original.mp4", encoder="libx264")
    first = cut_output(original, [(1_000_000, 1_500_000)], Event(), lambda value: None)
    music = replace_music(root, first, folder, 1, None)
    second = cut_output(music, [(500_000, 800_000)], Event(), lambda value: None)
    expected = cut_history(second)
    # New snapshots are self-contained even when intermediate sidecars disappear.
    from sculptcut.export_storage import metadata_path

    metadata_path(first, ".variant.json").unlink()
    result = replace_intro(root, second, new_image, None, encoder="libx264")
    expected_frames = frame_count(second) + (0 if had_intro else 22)
    assert expected_frames == 58
    assert_trimmed_output(result, expected_frames)
    if not had_intro:
        expected = [[(a + 733_333, b + 733_333) for a, b in batch] for batch in expected]
    assert cut_history(result) == expected
    assert read_variant(result)["plan"]["decisions"] == read_variant(original)["plan"]["decisions"]
    capture = cv2.VideoCapture(str(result))
    try:
        ok, frame = capture.read()
        assert ok and frame[:, :, 2].mean() > frame[:, :, 0].mean() * 4
    finally:
        capture.release()
    repeated = replace_intro(root, result, old_image, None, encoder="libx264")
    assert_trimmed_output(repeated, expected_frames)
    assert cut_history(repeated) == expected


def frame_count(video: Path) -> int:
    import json

    metadata = json.loads(run_process([
        discover("ffprobe"), "-v", "error", "-count_frames", "-select_streams", "v:0",
        "-show_entries", "stream=nb_read_frames", "-of", "json", str(video),
    ]))
    return int(metadata["streams"][0]["nb_read_frames"])


def assert_trimmed_output(video: Path, expected_frames: int) -> None:
    from sculptcut.media import probe

    assert frame_count(video) == expected_frames
    assert probe(video).audio_streams
    verify_output(video, expected_frames, 30, expected_audio=True)
    run_process([discover("ffmpeg"), "-v", "error", "-xerror", "-i", str(video), "-f", "null", "-"])
