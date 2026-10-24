"""Restored omitted episodes receive the preset's camera, including on reopening."""

from pathlib import Path
from threading import Event

import cv2
import numpy as np
import pytest

from sculptcut.combined_edits import prepare_combined_edit, render_combined_edit
from sculptcut.domain import CameraPoint, Override, Project, ProjectSettings, Roi, SubjectCamera
from sculptcut.episode_framing import frame_restored_episodes
from sculptcut.idle import IdleRange
from sculptcut.media import Cancelled, discover, run_process
from sculptcut.overrides import apply_overrides
from sculptcut.planning import RenderPlan, build_plan
from sculptcut.project import create_project, save_project
from sculptcut.rendering import render, verify_output
from sculptcut.shorts_layout import apply_square_layout
from sculptcut.variants import read_variant


def framing_fixture(tmp_path: Path, preset: str, *, blank_first: bool = False,
                    ) -> tuple[Path, Project, RenderPlan]:
    source = tmp_path / "source.mp4"
    background = "white" if preset.startswith("photoshop") else "0x464646"
    blank = ",drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='lt(t,1)'" if blank_first else ""
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 "color=c=red:s=320x180:r=30:d=3,"
                 f"drawbox=x=40:y=16:w=240:h=148:color={background}:t=fill,"
                 "drawbox=x=140:y=45:w=40:h=85:color=lime:t=fill" + blank,
                 "-c:v", "libx264", str(source)])
    root = tmp_path / "project"
    project = create_project(root, [source])
    source_id = project.sources[0].id
    project = project.model_copy(update={"settings": ProjectSettings.model_validate({
        "preset": preset, "target_seconds": 1,
        "shorts_square_rois": {source_id:Roi(x=.25, y=0, width=.5, height=1)},
    })})
    save_project(root, project)
    base = build_plan(project, [IdleRange(source_id=source_id, start_us=0,
                                         end_us=1_000_000, reason="auto:omission")], 1)
    viewport = Roi(x=.125, y=16/180, width=.75, height=148/180)
    camera = SubjectCamera(viewport=viewport, width=viewport.height * 9/16 * 180/320,
                           height=viewport.height,
                           points=[CameraPoint(time_us=1_000_000, x=.5, y=.5)], background=70)
    base = base.model_copy(update={
        "settings": base.settings | {"width":90, "height":160,
                                     "story":"music_episodes" if preset == "music_shorts"
                                     else "process"},
        "decisions":[d.model_copy(update={"crop":viewport,"subject_camera":camera})
                     if d.action != "DROP" else d for d in base.decisions],
    })
    return root, project, base


@pytest.mark.parametrize("preset", ["shorts", "music_shorts", "photoshop_shorts",
                                     "photoshop_music_shorts"])
def test_restored_partial_episode_is_framed_in_export_and_survives_reopening(
    tmp_path: Path, preset: str,
) -> None:
    root, project, base = framing_fixture(tmp_path, preset)
    parent = root / "renders/original.mp4"
    render(root, project, base, parent, Event(), encoder="libx264")
    original_decision = base.decisions[-1]
    source = project.sources[0]
    before = source.path.read_bytes()
    changed = project.model_copy(update={"overrides":[Override(source_id=source.id,
        start_us=250_000, end_us=750_000, action="SET_SPEED", speed=1)]})
    save_project(root, changed)
    edit = prepare_combined_edit(root, parent)
    restored = next(d for d in edit.plan.decisions if d.provenance == "user:SET_SPEED")
    assert restored.crop == "full" and restored.subject_camera is None
    expected = edit.plan
    output = root / "renders/fixed.mp4"
    render_combined_edit(root, edit, output, Event(), lambda _:None, encoder="libx264")
    verify_output(output, expected.actual_frame_count, expected.output_fps)
    state = read_variant(output)
    result = RenderPlan.model_validate(state["plan"])
    visible = next(d for d in result.decisions if d.provenance == "user:SET_SPEED")
    assert visible.subject_camera is not None
    if preset == "music_shorts":
        assert visible.subject_camera.viewport == original_decision.subject_camera.viewport
        assert visible.subject_camera.height == pytest.approx(
            visible.subject_camera.viewport.height,
        )
        assert visible.subject_camera.width <= visible.subject_camera.viewport.width
    assert visible.source_start_us == restored.source_start_us
    assert visible.source_end_us == restored.source_end_us
    assert visible.output_frame_count == restored.output_frame_count
    assert result.actual_frame_count == expected.actual_frame_count
    assert result.decisions[-1].subject_camera == original_decision.subject_camera
    reopened = prepare_combined_edit(root, output)
    assert reopened.plan.decisions == result.decisions
    framed, neutral = frame_restored_episodes(root, changed, reopened.plan,
        reopened.base_plan, Event(), lambda _:pytest.fail("Already framed footage was reanalysed"))
    assert framed is reopened.plan and neutral is reopened.base_plan
    save_project(root, project)
    undone = prepare_combined_edit(root, output)
    assert all(d.action == "DROP" for d in undone.plan.decisions if d.source_end_us <= 1_000_000)
    save_project(root, changed)
    assert prepare_combined_edit(root, output).plan.decisions == result.decisions
    assert source.path.read_bytes() == before
    if not preset.startswith("photoshop"):
        capture = cv2.VideoCapture(str(output))
        try:
            ok, image = capture.read()
            assert ok
            red = (image[:,:,2].astype(float) > image[:,:,1].astype(float) * 1.5 + 35)
            assert np.mean(red) < .02  # Red source interface must stay outside the camera.
        finally:
            capture.release()


@pytest.mark.parametrize("preset", ["timelapse", "speed", "shorts", "music_shorts",
    "beat_shorts", "photoshop_shorts", "photoshop_music_shorts", "photoshop_beat_shorts"])
def test_existing_framing_and_unedited_plans_are_unchanged(tmp_path: Path, preset: str) -> None:
    root, project, base = framing_fixture(tmp_path, preset)
    plan, neutral = frame_restored_episodes(root, project, base, base, Event(),
        lambda _:pytest.fail("Unedited footage was reanalysed"))
    assert plan is base and neutral is base


def test_restored_episode_framing_cancellation_keeps_inputs(tmp_path: Path) -> None:
    root, project, base = framing_fixture(tmp_path, "music_shorts")
    project = project.model_copy(update={"overrides":[Override(source_id=project.sources[0].id,
        start_us=0, end_us=1_000_000, action="SET_SPEED", speed=2)]})
    edited = apply_overrides(project, base, manual=True)
    cancel = Event()
    cancel.set()
    before = base.model_dump_json(), edited.model_dump_json()
    with pytest.raises(Cancelled):
        frame_restored_episodes(root, project, edited, base, cancel, lambda _:None)
    assert before == (base.model_dump_json(), edited.model_dump_json())


def test_restored_blank_interval_is_not_silently_deleted_again(tmp_path: Path) -> None:
    root, project, base = framing_fixture(tmp_path, "music_shorts", blank_first=True)
    project = project.model_copy(update={"overrides":[Override(source_id=project.sources[0].id,
        start_us=0, end_us=1_000_000, action="SET_SPEED", speed=2)]})
    edited = apply_overrides(project, base, manual=True)
    fixed, neutral = frame_restored_episodes(root, project, edited, base, Event(), lambda _:None)
    assert fixed.actual_frame_count == edited.actual_frame_count
    assert fixed.decisions[0].action == "SPEED"
    assert fixed.decisions[0].source_start_us == 0
    assert fixed.decisions[0].source_end_us == 1_000_000
    assert fixed.decisions[0].output_frame_count == edited.decisions[0].output_frame_count
    assert fixed.decisions[0].subject_camera is not None
    assert neutral.decisions[0].action == "DROP"


@pytest.mark.parametrize("version", [0, 2])
def test_previously_saved_zoomed_out_music_episode_is_repaired_once(
    tmp_path: Path, version: int,
) -> None:
    root, project, base = framing_fixture(tmp_path, "music_shorts")
    project = project.model_copy(update={"overrides":[Override(source_id=project.sources[0].id,
        start_us=0, end_us=1_000_000, action="SET_SPEED", speed=2)]})
    edited = apply_overrides(project, base, manual=True)
    viewport = base.decisions[-1].subject_camera.viewport
    wrong = SubjectCamera(viewport=viewport, fit_subject=True, width=1.4, height=4.4,
        points=[CameraPoint(time_us=0,x=.5,y=.5)], background=70)
    legacy = edited.model_copy(update={"settings":edited.settings | {
        "episode_framing_version":version,
    }, "decisions":[
        d.model_copy(update={"crop":viewport,"subject_camera":wrong}) if d.action == "SPEED"
        and d.provenance == "user:SET_SPEED" else d for d in edited.decisions],
    })
    neutral = base.model_copy(update={"decisions":[
        d.model_copy(update={"crop":viewport,"subject_camera":wrong}) if d.action == "DROP"
        else d for d in base.decisions],
    })
    fixed, neutral = frame_restored_episodes(root,project,legacy,neutral,Event(),lambda _:None)
    camera = fixed.decisions[0].subject_camera
    assert camera is not None
    assert camera.height == pytest.approx(camera.viewport.height)
    assert camera.width <= camera.viewport.width
    assert fixed.decisions[-1] == legacy.decisions[-1]
    assert [d.output_frame_count for d in fixed.decisions] == [d.output_frame_count
                                                            for d in legacy.decisions]
    repeated, repeated_base = frame_restored_episodes(root,project,fixed,neutral,Event(),
        lambda _:pytest.fail("Corrected saved camera was analysed again"))
    assert repeated is fixed and repeated_base is neutral


def test_correct_square_restored_frame_is_not_migrated(tmp_path: Path) -> None:
    root, project, base = framing_fixture(tmp_path,"music_shorts")
    project = project.model_copy(update={"settings":project.settings.model_copy(update={
        "shorts_layout":"square",
    }), "overrides":[Override(source_id=project.sources[0].id,start_us=0,end_us=1_000_000,
                              action="SET_SPEED",speed=2)]})
    base = apply_square_layout(project,base)
    edited = apply_overrides(project,base,manual=True)
    fixed,neutral = frame_restored_episodes(root,project,edited,base,Event(),
        lambda _:pytest.fail("The existing selected square was recalculated"))
    assert fixed is edited and neutral is base
