"""Decoded audio envelopes at the final export end, including both bookends."""

from pathlib import Path
from threading import Event
from typing import get_args

import numpy as np
import pytest
from test_intro import make_image

from sculptcut.audio import music_filter
from sculptcut.domain import ProjectSettings
from sculptcut.intro import body_frame_budget
from sculptcut.media import discover, run_process
from sculptcut.planning import build_plan
from sculptcut.project import create_project
from sculptcut.rendering import render, verify_output


@pytest.fixture(scope="module")
def media(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, Path]:
    folder = tmp_path_factory.mktemp("fade-out-media")
    video, music, image = (folder / name for name in ("source.mp4", "music.wav", "cover.png"))
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 "color=c=blue:s=160x90:r=30:d=8", "-c:v", "libx264", str(video)])
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 "sine=frequency=440:duration=8", str(music)])
    make_image(image, (0, 220, 0))
    return video, music, image


@pytest.mark.parametrize("preset", get_args(ProjectSettings.model_fields["preset"].annotation))
def test_all_modes_fade_over_last_five_seconds_of_complete_export(
    tmp_path: Path, media: tuple[Path, Path, Path], preset: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video, music, image = media
    # Only the delivery resolution is reduced; encoding, bookends, AAC and decoding are real.
    monkeypatch.setattr("sculptcut.rendering.export_size", lambda *args: (320, 180))
    settings = ProjectSettings.model_validate({
        "preset": preset, "music_path": music, "presentation_video": video,
        "presentation_seconds": .5, "intro_image": image, "intro_position": "end",
        "intro_seconds": 1, "intro_transition_seconds": .25,
    })
    project = create_project(tmp_path / "project", [video]).model_copy(
        update={"settings": settings},
    )
    frames = body_frame_budget(settings, 30, 8)
    plan = build_plan(project, [], frames / 30)
    output = render(tmp_path / "project", project, plan, tmp_path / "export.mp4",
                    encoder="libx264")
    verify_output(output, 240, 30, expected_audio=True)
    pcm = tmp_path / "decoded.f32"
    run_process([discover("ffmpeg"), "-v", "error", "-i", str(output), "-vn",
                 "-ac", "1", "-ar", "48000", "-f", "f32le", str(pcm)])
    samples = np.fromfile(pcm, dtype="<f4")

    def rms(seconds: float) -> float:
        start = round(seconds * 48000)
        return float(np.sqrt(np.mean(samples[start:start + 2400] ** 2)))

    baseline = rms(2.5)
    assert baseline > .01
    assert .95 < rms(2.9) / baseline < 1.05  # No premature fade before the final five seconds.
    levels = [rms(t) / baseline for t in (3.1, 4, 5, 6, 7, 7.9)]
    assert all(a > b for a, b in zip(levels, levels[1:], strict=False)), levels
    assert levels[0] > .95 and levels[2] < .75 and levels[4] < .2 and levels[5] < .02


@pytest.mark.parametrize("seconds", [.5, 2, 4, 5, 8, 60])
def test_fade_out_is_bounded_for_short_outputs(seconds: float) -> None:
    result = music_filter(seconds, 2)
    fade = min(5, seconds)
    assert f"afade=t=out:st={seconds - fade:.9f}:d={fade:.9f}:curve=hsin" in result
    assert "afade=t=in:st=0:d=" in result


def test_fade_follows_new_end_after_trim_music_and_cover_edits(
    tmp_path: Path, media: tuple[Path, Path, Path],
) -> None:
    from sculptcut.media import probe
    from sculptcut.output_cuts import cut_output
    from sculptcut.project import save_project
    from sculptcut.reintro import replace_intro
    from sculptcut.remusic import replace_music

    video, music, image = media
    root = tmp_path / "project"
    project = create_project(root, [video]).model_copy(update={"settings": ProjectSettings(
        preset="speed", music_path=music, music_fade_seconds=0,
        intro_seconds=1, intro_transition_seconds=.25,
    )})
    save_project(root, project)
    plan = build_plan(project, [], 8)
    original = render(root, project, plan, root / "renders/original.mp4", encoder="libx264")
    trimmed = cut_output(original, [(1_000_000, 3_000_000)], Event(), lambda _: None)
    alternate = tmp_path / "alternate.wav"
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 "sine=frequency=880:duration=8", str(alternate)])
    remixed = replace_music(root, trimmed, alternate, 1, None)
    covered = replace_intro(root, remixed, image, None, encoder="libx264",
                            intro_options={"intro_position": "end"})
    for index, (output, frames) in enumerate(((original, 240), (trimmed, 180),
                                               (remixed, 180), (covered, 202))):
        verify_output(output, frames, 30, expected_audio=True)
        assert probe(output).audio_streams
        pcm = tmp_path / f"edited-{index}.f32"
        run_process([discover("ffmpeg"), "-v", "error", "-i", str(output), "-vn",
                     "-ac", "1", "-ar", "48000", "-f", "f32le", str(pcm)])
        samples = np.fromfile(pcm, dtype="<f4")

        def rms(seconds: float, data: np.ndarray = samples) -> float:
            start = round(seconds * 48000)
            return float(np.sqrt(np.mean(data[start:start + 2400] ** 2)))

        fade_start = frames / 30 - 5
        baseline = rms(fade_start - .1)
        levels = [rms(fade_start + t) / baseline for t in (.1, 1, 2, 3, 4, 4.9)]
        assert baseline > .01
        assert all(a > b for a, b in zip(levels, levels[1:], strict=False)), (output, levels)
        assert levels[0] > .95 and levels[2] < .75 and levels[4] < .2 and levels[5] < .02
