from pathlib import Path

import pytest
from test_beat_shorts import pulse_track
from test_intro import make_image

from sculptcut.analysis import extract_features
from sculptcut.domain import Project, ProjectSettings
from sculptcut.fixtures import generate_fixture
from sculptcut.intro import body_frame_budget, body_start_frame, output_frame_count
from sculptcut.media import discover, run_process
from sculptcut.planning import plan_project
from sculptcut.project import create_project, fingerprint, save_project
from sculptcut.proxy import make_proxies
from sculptcut.rendering import render, verify_output
from sculptcut.shorts_layout import PHOTOSHOP_PRESETS, SHORTS_PRESETS

PRESETS = ("timelapse", "speed", *SHORTS_PRESETS)


@pytest.mark.parametrize("bookend", ["none", "intro", "start", "end"])
@pytest.mark.parametrize("fps", [1, 24, 30, 60, 120])
def test_bookends_use_the_total_integer_frame_budget(bookend: str, fps: int) -> None:
    settings = ProjectSettings.model_validate({
        "preset": "speed", "output_fps": fps,
        "intro_image": Path("unused.png") if bookend != "none" else None,
        "intro_seconds": 2, "intro_transition_seconds": .5,
        "presentation_video": Path("clip.mp4") if bookend in ("start", "end") else None,
        "presentation_position": "end" if bookend == "end" else "start",
    })
    frames = body_frame_budget(settings, fps, 60)
    assert output_frame_count(settings, fps, frames) == 60 * fps
    assert frames > 0
    if bookend == "intro" and fps == 30:
        assert frames == 1755  # 58.5 seconds of body + 1.5 seconds of effective cover.
    if bookend in ("start", "end"):
        assert frames == 55 * fps  # Presentation wins over the inactive image.
        assert body_start_frame(settings, fps, frames) == (5 * fps if bookend == "start" else 0)


@pytest.mark.parametrize("seconds", [.01, .5, 1, 1.5])
def test_impossible_intro_budget_is_rejected_before_source_analysis(seconds: float) -> None:
    settings = ProjectSettings(intro_image=Path("unused.png"))
    with pytest.raises(ValueError, match="не хватает"):
        body_frame_budget(settings, 30, seconds)
    with pytest.raises(ValueError, match="не хватает"):
        plan_project(Path("not-a-project"), Project(sources=[], settings=settings), seconds)


@pytest.fixture(scope="module")
def media(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, Path]:
    directory = tmp_path_factory.mktemp("duration-media")
    sample = generate_fixture(directory / "sample.mp4", "local", duration=3, fps=10)
    video = directory / "source.mp4"
    run_process([discover("ffmpeg"), "-v", "error", "-stream_loop", "7", "-i", str(sample),
                 "-c", "copy", str(video)])
    image, music = directory / "cover.png", directory / "pulse.wav"
    make_image(image, (0, 220, 0))
    pulse_track(music, [.25 + i * .5 for i in range(26)], seconds=14)
    return video, image, music


@pytest.mark.parametrize("preset", PRESETS)
def test_every_mode_exports_the_total_duration_with_intro(
    tmp_path: Path, media: tuple[Path, Path, Path], preset: str,
) -> None:
    video, image, music = media
    before = fingerprint(video)
    root = tmp_path / "project"
    created = create_project(root, [video])
    settings = ProjectSettings.model_validate({
        "preset": preset, "target_seconds": 12, "intro_image": image,
        "music_path": music, "music_fade_seconds": 0,
        "proxy_width": 192, "proxy_fps": 5,
    })
    project = created.model_copy(update={"settings": settings})
    save_project(root, project)
    if preset not in ("speed", *PHOTOSHOP_PRESETS):
        extract_features(root, project)
    plan = plan_project(root, project, 12)
    assert plan.target_frame_count == plan.actual_frame_count == 315
    assert plan.settings["target_seconds"] == 12
    assert plan.settings["requested_output_frames"] == 360
    from sculptcut.edit_stats import statistics_text

    assert plan.settings["actual_output_frames"] == 360
    assert "Ролик: 00:12" in statistics_text(plan)
    assert plan_project(root, project, 12) == plan
    output = render(root, project, plan, tmp_path / f"{preset}.mp4", encoder="libx264")
    verify_output(output, 360, 30, expected_audio=True)
    assert fingerprint(video) == before


@pytest.mark.parametrize("position", ["start", "end"])
def test_presentation_and_missing_inactive_cover_fit_total_duration(
    tmp_path: Path, media: tuple[Path, Path, Path], position: str,
) -> None:
    video, _, music = media
    root = tmp_path / "project"
    project = create_project(root, [video])
    project = project.model_copy(update={"settings": ProjectSettings(
        preset="speed", target_seconds=6, presentation_video=video,
        presentation_seconds=2, presentation_position=position,
        intro_image=tmp_path / "missing-inactive.png", music_path=music,
    )})
    save_project(root, project)
    plan = plan_project(root, project, 6)
    assert plan.actual_frame_count == 120
    make_proxies(root, project)
    for preview in (False, True):
        output = render(root, project, plan, tmp_path / f"{position}-{preview}.mp4",
                        preview=preview, encoder="libx264")
        verify_output(output, 180, 30, expected_audio=True)


def test_adding_intro_to_new_untrimmed_variant_keeps_total_duration(
    tmp_path: Path, media: tuple[Path, Path, Path],
) -> None:
    from sculptcut.reintro import replace_intro

    video, image, music = media
    root = tmp_path / "project"
    project = create_project(root, [video]).model_copy(update={"settings": ProjectSettings(
        preset="speed", target_seconds=6, music_path=music,
    )})
    save_project(root, project)
    plan = plan_project(root, project, 6)
    original = render(root, project, plan, root / "renders/original.mp4", encoder="libx264")
    changed = replace_intro(root, original, image, None, encoder="libx264")
    verify_output(original, 180, 30, expected_audio=True)
    verify_output(changed, 180, 30, expected_audio=True)


def test_regular_rhythm_uses_total_soundtrack_and_intro_offset(
    tmp_path: Path, media: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sculptcut.planning import build_plan
    from sculptcut.rhythm import align_plan

    video, image, music = media
    project = create_project(tmp_path / "project", [video]).model_copy(update={
        "settings": ProjectSettings(intro_image=image, music_path=music, music_fade_seconds=0),
    })
    base = build_plan(project, [], 6.5)
    original = base.decisions[0]
    base = base.model_copy(update={"decisions": [original.model_copy(update={
        "source_end_us": 9_000_000, "output_frame_count": 90,
    }), original.model_copy(update={
        "source_start_us": 9_000_000, "output_frame_count": 105,
    })]})
    durations = []

    def soundtrack(root, settings, seconds, cancel):
        durations.append(seconds)
        return music

    def accents(path, seconds, loop, cancel):
        durations.append(seconds)
        return [4.6]  # 1.5-second effective intro + 3.1-second body boundary.

    monkeypatch.setattr("sculptcut.rhythm.soundtrack", soundtrack)
    monkeypatch.setattr("sculptcut.rhythm.accents", accents)
    result = align_plan(tmp_path, project, base, None)
    assert durations == [8, 8]
    assert result.decisions[0].output_frame_count == 93
    assert result.actual_frame_count == 195
