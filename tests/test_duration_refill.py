"""All presets restore bookends while preserving cuts on the original edit clock."""

from pathlib import Path
from threading import Event
from typing import get_args

import cv2
import pytest
from test_intro import make_image
from test_timeline import make_source

from sculptcut.domain import Project, ProjectSettings
from sculptcut.export_storage import metadata_path
from sculptcut.intro import body_frame_budget, output_frame_count
from sculptcut.media import discover, run_process
from sculptcut.output_cuts import cut_output
from sculptcut.planning import RenderDecision, build_plan, validate_plan
from sculptcut.project import create_project, save_project
from sculptcut.rendering import render, verify_output
from sculptcut.shorts_refill import (
    cuts_on_plan_clock,
    rebuild_after_cuts,
    refill_plan_after_cuts,
    refill_render_context,
)
from sculptcut.variants import cut_history, read_variant

PRESETS = get_args(ProjectSettings.model_fields["preset"].annotation)


@pytest.mark.parametrize("action", ["HOLD", "MONTAGE"])
def test_static_cuts_preserve_sample_timestamps_and_source_coverage(action: str) -> None:
    project = Project(sources=[make_source(0, 0, 12_000_000)],
                      settings=ProjectSettings(preset="timelapse"))
    plan = build_plan(project, [], 3)
    decision = plan.decisions[0].model_copy(update={
        "action": action, "provenance": "user:test",
        "sample_times_us": [1_000_000, 5_000_000, 9_000_000] if action == "MONTAGE" else [],
    })
    plan = plan.model_copy(update={"decisions": [decision]})
    rebuilt = refill_plan_after_cuts(project, plan, [(1_000_000, 2_000_000)])
    validate_plan(project, rebuilt)
    assert rebuilt.actual_frame_count == 90
    held = [d.source_start_us for d in rebuilt.decisions if d.action == "HOLD"]
    assert held == ([1_000_000, 9_000_000] if action == "MONTAGE" else [0])
    assert all(d.action != "MONTAGE" for d in rebuilt.decisions)


@pytest.mark.parametrize("cuts", [[(0, 1_000_000)], [(0, 1_800_000)],
                                  [(0, 900_000), (500_000, 1_800_000)]])
def test_intro_is_restored_and_only_body_intersection_is_excluded(
    tmp_path: Path, cuts: list[tuple[int, int]],
) -> None:
    image = tmp_path / "intro.png"
    make_image(image, (0, 220, 0))
    settings = ProjectSettings(preset="timelapse", intro_image=image)
    project = Project(sources=[make_source(0, 0, 12_000_000)], settings=settings)
    budget = body_frame_budget(settings, 30, 3)
    plan = build_plan(project, [], budget / 30)
    rebuilt = refill_plan_after_cuts(project, plan, cuts)
    assert output_frame_count(settings, 30, rebuilt.actual_frame_count) == 90
    if cuts[0][1] == 1_000_000:
        assert rebuilt.decisions == plan.decisions
    else:
        removed = [d for d in rebuilt.decisions if d.provenance == "user:shorts_manual_cut"]
        assert [(d.source_start_us, d.source_end_us) for d in removed] == [(0, 2_400_000)]


@pytest.mark.parametrize("preset", PRESETS)
def test_real_all_presets_restore_intro_and_duration_with_saved_music(
    tmp_path: Path, preset: str,
) -> None:
    source = tmp_path / "source.mp4"
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 "testsrc2=s=160x120:r=30:d=6", "-c:v", "libx264", str(source)])
    image = tmp_path / "intro.png"
    make_image(image, (0, 220, 0))
    music = tmp_path / "music.wav"
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 "sine=frequency=440:duration=3", str(music)])
    root = tmp_path / "project"
    settings = ProjectSettings.model_validate({
        "preset": preset, "intro_image": image, "music_path": music, "target_seconds": 3,
    })
    project = create_project(root, [source]).model_copy(update={"settings": settings})
    save_project(root, project)
    plan = build_plan(project, [], body_frame_budget(settings, 30, 3) / 30)
    if preset in ("beat_shorts", "photoshop_beat_shorts"):
        decision = plan.decisions[0]
        decisions = [decision.model_copy(update={
            "source_start_us": index * 2_000_000,
            "source_end_us": (index + 1) * 2_000_000,
            "action": "HOLD", "provenance": "user:test", "output_frame_count": 15,
        }) for index in range(3)]
        plan = plan.model_copy(update={"decisions": decisions})
    elif preset == "timelapse":
        plan = plan.model_copy(update={"decisions": [plan.decisions[0].model_copy(update={
            "action": "MONTAGE", "sample_times_us": [0, 2_000_000, 4_000_000],
        })]})
    plan = plan.model_copy(update={"settings": plan.settings | {"width": 160, "height": 120}})
    original = root / "renders" / "original.mp4"
    render(root, project, plan, original, Event(), encoder="libx264")
    protected = {path: path.read_bytes() for path in
                 (source, image, original, metadata_path(original, ".variant.json"),
                  root / "project.json")}
    music.unlink()  # The export's saved music must be sufficient, including GUI preflight.
    context, saved_plan = refill_render_context(original, project, plan)
    validate_plan(context, saved_plan)
    result = rebuild_after_cuts(root, original, [(0, 1_000_000), (1_700_000, 1_900_000)],
                                Event(), lambda _: None, encoder="libx264")
    verify_output(result, 90, 30, expected_audio=True)
    state = read_variant(result)
    assert state["operation"] == "duration_refill" and cut_history(result) == []
    capture = cv2.VideoCapture(str(result))
    for index in (0, 29, 43):
        capture.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, frame = capture.read()
        assert ok
        # All of the previously removed second of green cover has been rebuilt.
        assert frame[60, 80, 1] > 150
    capture.release()
    if preset == "speed":
        # A second refill must neither reapply old cuts nor require original music.
        repeated = rebuild_after_cuts(root, result, [(2_100_000, 2_200_000)], Event(),
                                      lambda _: None, encoder="libx264")
        verify_output(repeated, 90, 30, expected_audio=True)
        assert cut_history(repeated) == []
    assert all(path.read_bytes() == before for path, before in protected.items())


def test_refill_after_successive_plain_trims_uses_original_plan_clock(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 "testsrc2=s=160x120:r=30:d=6", "-c:v", "libx264", str(source)])
    root = tmp_path / "project"
    project = create_project(root, [source])
    save_project(root, project)
    plan = build_plan(project, [], 3)
    plan = plan.model_copy(update={"settings": plan.settings | {"width": 160, "height": 120}})
    original = root / "renders" / "original.mp4"
    render(root, project, plan, original, Event(), encoder="libx264")
    first = cut_output(original, [(500_000, 1_000_000)], Event(), lambda _: None)
    second = cut_output(first, [(1_000_000, 1_500_000)], Event(), lambda _: None)
    result = rebuild_after_cuts(root, second, [(250_000, 500_000)], Event(),
                                lambda _: None, encoder="libx264")
    verify_output(result, 90, 30)
    state = read_variant(result)
    assert state["cuts_us"] == [[250_000, 1_000_000], [1_500_000, 2_000_000]]
    assert cut_history(result) == []
    decisions = [RenderDecision.model_validate(d) for d in state["plan"]["decisions"]]
    # 0.25 seconds begins between output frames: the cut includes frame 7.
    assert [(d.source_start_us, d.source_end_us) for d in decisions if d.action == "DROP"] == [
        (466_666, 2_000_000), (3_000_000, 4_000_000),
    ]


@pytest.mark.parametrize("position", ["start", "end"])
def test_real_presentation_is_restored_with_body_cuts(tmp_path: Path, position: str) -> None:
    source = tmp_path / "source.mp4"
    presentation = tmp_path / "presentation.mp4"
    for path, color in ((source, "blue"), (presentation, "green")):
        run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                     f"color=c={color}:s=160x120:r=30:d=6", "-c:v", "libx264", str(path)])
    root = tmp_path / "project"
    settings = ProjectSettings.model_validate({
        "preset": "speed", "presentation_video": presentation,
        "presentation_seconds": 1, "presentation_position": position,
    })
    project = create_project(root, [source]).model_copy(update={"settings": settings})
    save_project(root, project)
    plan = build_plan(project, [], 2)
    plan = plan.model_copy(update={"settings": plan.settings | {"width": 160, "height": 120}})
    original = root / "renders" / "original.mp4"
    render(root, project, plan, original, Event(), encoder="libx264")
    cuts = [(0, 1_200_000)] if position == "start" else [(1_800_000, 3_000_000)]
    result = rebuild_after_cuts(root, original, cuts, Event(), lambda _: None, encoder="libx264")
    verify_output(result, 90, 30)
    capture = cv2.VideoCapture(str(result))
    capture.set(cv2.CAP_PROP_POS_FRAMES, 0 if position == "start" else 89)
    ok, frame = capture.read()
    capture.release()
    assert ok and frame[60, 80, 1] > 90 and frame[60, 80, 0] < 10
    assert original.exists() and presentation.exists()


@pytest.mark.parametrize("cuts", [[], [(-1, 1)], [(1, 1)], [(0, 4_000_000)]])
def test_invalid_cuts_are_rejected(cuts: list[tuple[int, int]]) -> None:
    project = Project(sources=[make_source(0, 0, 12_000_000)])
    with pytest.raises(ValueError, match="границы"):
        refill_plan_after_cuts(project, build_plan(project, [], 3), cuts)


def test_cut_to_last_frame_accepts_player_millisecond_rounding(tmp_path: Path) -> None:
    from sculptcut.variants import save_variant

    project = Project(sources=[make_source(0, 0, 12_000_000)])
    plan = build_plan(project, [], 77 / 30)
    video = tmp_path / "original.mp4"
    save_variant(video, project=project, plan=plan)
    # Qt displays 77/30 seconds as 2567 ms; the saved plan ends at 2566666.67 us.
    cuts = cuts_on_plan_clock(video, project, plan, [(2_500_000, 2_567_000)])
    rebuilt = refill_plan_after_cuts(project, plan, cuts)
    validate_plan(project, rebuilt)
    assert rebuilt.actual_frame_count == 77
    assert rebuilt.decisions[-1].action == "DROP"
    with pytest.raises(ValueError, match="границы"):
        cuts_on_plan_clock(video, project, plan, [(2_500_000, 2_568_000)])
