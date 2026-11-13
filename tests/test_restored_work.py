"""Returning a grey block keeps work, excludes idle and never grows the music edit."""

from pathlib import Path
from threading import Event

import cv2
import numpy as np
import pytest
from PySide6.QtWidgets import QApplication
from test_beat_shorts import pulse_track
from test_timeline import make_source

from sculptcut.analysis import extract_features
from sculptcut.combined_edits import (
    apply_episode_edits,
    prepare_combined_edit,
    render_combined_edit,
)
from sculptcut.domain import Override, Project, ProjectSettings, Roi
from sculptcut.episode_editor import EpisodeEditor, edit_episode
from sculptcut.motion import analyze_motion
from sculptcut.planning import RenderDecision, RenderPlan, build_plan, plan_project, validate_plan
from sculptcut.project import create_project, save_project
from sculptcut.rendering import render, verify_output
from sculptcut.restored_work import find_work_windows, fit_edit_budget, trim_budget_windows
from sculptcut.shorts_layout import SHORTS_PRESETS
from sculptcut.variants import read_variant


def test_infeasible_manual_speed_recuts_source_instead_of_extending_or_rejecting() -> None:
    project = Project(
        sources=[make_source(0, 0, 10_000_000)],
        settings=ProjectSettings(
            preset="music_shorts",
            target_seconds=2,
        ),
        overrides=[
            Override(source_id="0", start_us=0, end_us=10_000_000, action="SET_SPEED", speed=1)
        ],
    )
    plan = build_plan(project, [], 2)
    result = apply_episode_edits(project, plan)
    assert result.actual_frame_count == 60
    assert not any("infeasible" in w for w in result.warnings)
    kept = [d for d in result.decisions if d.output_frame_count]
    assert len(kept) == 1 and kept[0].source_end_us - kept[0].source_start_us == 2_000_000
    assert apply_episode_edits(project, result).decisions == result.decisions


def test_unanalysed_restore_fails_instead_of_returning_the_whole_grey_block(tmp_path: Path) -> None:
    project = Project(sources=[make_source(0, 0, 300_000_000)])
    rule = Override(
        source_id="0",
        start_us=0,
        end_us=300_000_000,
        action="SET_SPEED",
        speed=8,
        restore_omitted=True,
    )
    with pytest.raises(ValueError, match="анализ"):
        find_work_windows(tmp_path, project, rule)


def test_restored_selection_survives_switching_between_shorts_and_timelapse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def select(*args):
        calls.append(True)
        assert len(calls) == 1
        return [(80_000_000,86_000_000)]

    monkeypatch.setattr("sculptcut.restored_work.find_work_windows",select)
    project = Project(sources=[make_source(0,0,300_000_000)],overrides=[Override(
        source_id="0",start_us=60_000_000,end_us=240_000_000,
        action="SET_SPEED",speed=8,restore_omitted=True,
    )])
    selection = None
    for preset in ("shorts","timelapse","music_shorts"):
        current = project.model_copy(update={"settings":ProjectSettings.model_validate({
            "preset":preset,"target_seconds":12,
        })})
        base = build_plan(current,[],12)
        if selection is not None:
            base = base.model_copy(update={"settings":base.settings | {
                "restored_work_selection":selection,
            }})
        plan = apply_episode_edits(current,base,root=tmp_path)
        assert plan.actual_frame_count == 360
        assert all(not d.output_frame_count or d.source_end_us <= 60_000_000
                   or 80_000_000 <= d.source_start_us < d.source_end_us <= 86_000_000
                   or d.source_start_us >= 240_000_000 for d in plan.decisions)
        selection = plan.settings["restored_work_selection"]
    assert len(calls) == 1


def test_excess_episodes_are_reselected_with_work_priority_and_minimum_duration() -> None:
    project = Project(
        sources=[make_source(0, 0, 10_000_000)],
        settings=ProjectSettings(
            preset="music_shorts",
            target_seconds=0.8,
        ),
    )
    plan = build_plan(project, [], 0.8).model_copy(
        update={
            "actual_frame_count": 300,
            "decisions": [
                RenderDecision(
                    source_id="0",
                    source_start_us=i * 1_000_000,
                    source_end_us=(i + 1) * 1_000_000,
                    action="SPEED",
                    output_frame_count=30,
                    provenance="user:restored_work" if i == 7 else "auto:episode",
                )
                for i in range(10)
            ],
        }
    )
    result = trim_budget_windows(project, fit_edit_budget(project, plan, 24))
    from sculptcut.planning import validate_plan

    validate_plan(project, result)
    kept = [d for d in result.decisions if d.output_frame_count]
    assert len(kept) == 2
    assert all(d.output_frame_count >= 12 for d in kept)
    assert any(d.provenance == "user:restored_work" for d in kept)
    assert sum(d.output_frame_count for d in kept) == 24
    assert result == trim_budget_windows(project, fit_edit_budget(project, plan, 24))


@pytest.mark.parametrize("preset", ["timelapse", "speed", *SHORTS_PRESETS])
def test_every_mode_recuts_overflow_preserving_exclusions_and_verified_finale(preset: str) -> None:
    project = Project(
        sources=[make_source(0, 0, 10_000_000)],
        settings=ProjectSettings.model_validate({"preset": preset}),
        overrides=[
            Override(
                source_id="0", start_us=1_000_000, end_us=9_000_000, action="SET_SPEED", speed=1
            )
        ],
    )
    plan = build_plan(project, [], 2)
    plan = plan.model_copy(
        update={
            "actual_frame_count": 240,
            "settings": plan.settings | {"bookends": True, "final_view_frames": 60},
            "decisions": [
                RenderDecision(
                    source_id="0",
                    source_start_us=0,
                    source_end_us=1_000_000,
                    action="DROP",
                    output_frame_count=0,
                    provenance="user:shorts_manual_cut",
                ),
                RenderDecision(
                    source_id="0",
                    source_start_us=1_000_000,
                    source_end_us=9_000_000,
                    action="SPEED",
                    output_frame_count=180,
                    provenance="user:SET_SPEED",
                ),
                RenderDecision(
                    source_id="0",
                    source_start_us=9_000_000,
                    source_end_us=10_000_000,
                    action="HOLD",
                    output_frame_count=60,
                    provenance="user:final_tail",
                ),
            ],
        }
    )
    validate_plan(project, plan)
    result = apply_episode_edits(project, plan)
    validate_plan(project, result)
    assert result.actual_frame_count == result.target_frame_count == 60
    assert result.decisions[0] == plan.decisions[0]
    final = result.decisions[-1]
    assert final.action == "HOLD" and final.provenance == "user:final_tail"
    assert final.source_start_us == 9_000_000 and final.source_end_us == 10_000_000
    assert result.settings["final_view_frames"] == final.output_frame_count
    assert not any("infeasible" in warning.lower() for warning in result.warnings)


def test_overflow_montage_reselects_existing_verified_samples() -> None:
    project = Project(sources=[make_source(0, 0, 10_000_000)])
    plan = build_plan(project, [], 1).model_copy(
        update={
            "actual_frame_count": 300,
            "decisions": [
                RenderDecision(
                    source_id="0",
                    source_start_us=0,
                    source_end_us=10_000_000,
                    action="MONTAGE",
                    output_frame_count=300,
                    sample_times_us=[1_000_000, 5_000_000, 9_000_000],
                    provenance="user:montage",
                )
            ],
        }
    )
    validate_plan(project, plan)
    fitted = trim_budget_windows(project, fit_edit_budget(project, plan, 24))
    validate_plan(project, fitted)
    kept = [d for d in fitted.decisions if d.output_frame_count]
    assert len(kept) == 2
    assert all(
        d.action == "HOLD" and d.source_start_us in (1_000_000, 5_000_000, 9_000_000) for d in kept
    )
    assert fitted.actual_frame_count == fitted.target_frame_count == 24


@pytest.mark.parametrize("preset", ["music_shorts", "timelapse"])
def test_real_restore_stops_before_idle_preserves_budget_and_survives_reopening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preset: str,
) -> None:
    source = tmp_path / "work-and-idle.mp4"
    writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*"mp4v"), 10, (640, 360))
    assert writer.isOpened()
    for index in range(360):
        frame = np.full((360, 640, 3), 20, np.uint8)
        cv2.rectangle(frame, (80, 40), (559, 319), (70, 70, 70), -1)
        cv2.ellipse(frame, (320, 200), (60, 100), 0, 0, 360, (150, 150, 150), -1)
        growth = min(60, max(0, index - 60))
        if growth:
            cv2.rectangle(frame, (290, 150), (290 + growth, 250), (210, 210, 210), -1)
        writer.write(frame)
    writer.release()
    music = tmp_path / "music.wav"
    pulse_track(music, [0.5, 1, 1.5, 2, 2.5], seconds=3)
    root = tmp_path / "project"
    project = create_project(root, [source])
    source_id = project.sources[0].id
    viewport = Roi(x=80 / 640, y=40 / 360, width=480 / 640, height=280 / 360)
    project = project.model_copy(
        update={
            "settings": ProjectSettings(
                preset=preset,
                target_seconds=3,
                music_path=music,
                proxy_width=640,
                proxy_fps=5,
                final_view_seconds=0,
            ),
            "rois": {source_id: viewport},
        }
    )
    save_project(root, project)
    extract_features(root, project)
    analyze_motion(root, project)
    base = build_plan(project, [], 3).model_copy(
        update={
            "decisions": [
                RenderDecision(
                    source_id=source_id,
                    source_start_us=a * 1_000_000,
                    source_end_us=b * 1_000_000,
                    action=action,
                    output_frame_count=count,
                    provenance="user:process_omission"
                    if action == "DROP"
                    else "user:process_story",
                )
                for a, b, action, count in (
                    (0, 6, "SPEED", 45),
                    (6, 30, "DROP", 0),
                    (30, 36, "SPEED", 45),
                )
            ]
        }
    )
    base = base.model_copy(
        update={
            "settings": base.settings
            | {
                "story": "music_episodes" if preset == "music_shorts" else "static_episodes",
                "width": 90,
                "height": 160,
            }
        }
    )
    parent = root / "renders/parent.mp4"
    render(root, project, base, parent, Event(), encoder="libx264")
    changed = edit_episode(project, base.decisions[1], 8)
    save_project(root, changed)
    edit = prepare_combined_edit(root, parent)
    assert edit.plan.actual_frame_count == edit.plan.target_frame_count == 90
    restored = [d for d in edit.plan.decisions if d.provenance == "user:restored_work"]
    assert restored and all(
        6_000_000 <= d.source_start_us < d.source_end_us <= 13_000_000 for d in restored
    )
    assert sum(d.source_end_us - d.source_start_us for d in restored) <= 18_000_000
    assert all(d.output_frame_count >= 12 for d in restored)
    assert all(
        d.action == "DROP" or d.source_end_us <= 13_000_000 or d.source_start_us >= 30_000_000
        for d in edit.plan.decisions
    )
    # Plain Create must bound the restored range before automatic episode
    # selection as well, rather than fixing the already-selected long gap later.
    fresh_restored = plan_project(root, changed, 3)
    assert fresh_restored.actual_frame_count == fresh_restored.target_frame_count == 90
    assert "restored_work_selection" in fresh_restored.settings
    assert all(d.action == "DROP" or d.source_end_us <= 13_000_000
               or d.source_start_us >= 30_000_000 for d in fresh_restored.decisions)
    # The editor must not merge a returned work fragment with adjacent grey idle.
    app = QApplication.instance() or QApplication([])
    editor = EpisodeEditor()
    editor.context_settings = edit.project.settings
    editor.context_plan = edit.base_plan
    editor.load(root)
    for index in range(editor.count()):
        from PySide6.QtCore import Qt

        decision = editor.item(index).data(Qt.ItemDataRole.UserRole)
        assert (decision.action == "DROP") == (decision.output_frame_count == 0)
    editor.close()
    app.processEvents()
    output = root / "renders/restored.mp4"
    render_combined_edit(root, edit, output, Event(), lambda _: None, encoder="libx264")
    verify_output(output, 90, 30, expected_audio=True)
    saved = RenderPlan.model_validate(read_variant(output)["plan"])
    monkeypatch.setattr(
        "sculptcut.restored_work.find_work_windows",
        lambda *args: pytest.fail(
            "Unchanged saved work must reuse its bounded selection",
        ),
    )
    reopened = prepare_combined_edit(root, output)
    assert reopened.plan.decisions == saved.decisions
    assert reopened.plan.actual_frame_count == 90
    # A separate white-episode slowdown is automatically recut too; it cannot
    # produce a longer movie or a duration-conflict message.
    slowed = project.model_copy(
        update={
            "overrides": [
                Override(
                    source_id=source_id, start_us=0, end_us=6_000_000, action="SET_SPEED", speed=1
                )
            ]
        }
    )
    save_project(root, slowed)
    fitted = prepare_combined_edit(root, parent)
    assert fitted.plan.actual_frame_count == 90
    assert not any("infeasible" in w for w in fitted.plan.warnings)
    fitted_output = root / "renders/auto-fit.mp4"
    render_combined_edit(root, fitted, fitted_output, Event(), lambda _: None, encoder="libx264")
    verify_output(fitted_output, 90, 30, expected_audio=True)
    # Fresh Create, without an episode-neutral snapshot, must obey the same
    # budget when a saved speed constraint would otherwise extend the movie.
    full_slow = project.model_copy(
        update={
            "overrides": [
                Override(
                    source_id=source_id, start_us=0, end_us=36_000_000, action="SET_SPEED", speed=1
                )
            ]
        }
    )
    fresh = plan_project(root, full_slow, 3)
    assert fresh.actual_frame_count == 90
    assert fresh.settings.get("automatic_budget_fit") == 1
    assert not any("infeasible" in w for w in fresh.warnings)
    assert project.overrides == []
    assert not list(tmp_path.glob("*.variant.json"))
