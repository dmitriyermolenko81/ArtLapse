"""Deleting episodes cannot reapply old speeds to automatically omitted footage."""

from pathlib import Path

import pytest
from test_music_episodes import motion_plan
from test_music_override_rhythm import musical_plan

from sculptcut.audio_beat_sync import MusicEvent
from sculptcut.combined_edits import apply_episode_edits, creation_edit, prepare_combined_edit
from sculptcut.domain import Override, Project
from sculptcut.episode_editor import edit_episode
from sculptcut.music_episodes import retime_episodes
from sculptcut.overrides import apply_overrides
from sculptcut.planning import RenderPlan, validate_plan
from sculptcut.project import save_project
from sculptcut.variants import save_variant


def saved_montage(tmp_path: Path, preset: str = "music_shorts"
                  ) -> tuple[Project, RenderPlan, Path]:
    project, plan = musical_plan()
    project = project.model_copy(update={"settings": project.settings.model_copy(update={
        "preset": preset,
    }), "overrides": [Override(source_id="0", start_us=20_000_000,
                               end_us=160_000_000, action="SET_SPEED", speed=8)]})
    plan = plan.model_copy(update={"settings": plan.settings | {"preset": preset}})
    plan = apply_overrides(project, plan)
    save_project(tmp_path, project)
    video = tmp_path / "original.mp4"
    save_variant(video, project=project, plan=plan)
    return project, plan, video


def visible(plan: RenderPlan) -> list[tuple[int, int]]:
    return [(d.source_start_us, d.source_end_us) for d in plan.decisions if d.output_frame_count]


@pytest.mark.parametrize("preset", ["music_shorts", "photoshop_music_shorts"])
def test_delete_reopen_undo_redo_and_create_preserve_automatic_omissions(
    tmp_path: Path, preset: str,
) -> None:
    project, base, original = saved_montage(tmp_path, preset)
    selected = [d for d in base.decisions if d.output_frame_count]
    changed = edit_episode(project, selected[-1], None)
    save_project(tmp_path, changed)
    edit = prepare_combined_edit(tmp_path, original)
    assert visible(edit.plan) == visible(base)[:-1]
    assert all(d.output_frame_count >= 15 for d in edit.plan.decisions if d.output_frame_count)
    validate_plan(edit.project, edit.plan)
    corrected = tmp_path / "corrected.mp4"
    save_variant(corrected, project=edit.snapshot, plan=edit.plan, parent=original,
                 operation="combined_edit", episode_base_plan=edit.base_plan)
    assert prepare_combined_edit(tmp_path, corrected).plan.decisions == edit.plan.decisions
    save_project(tmp_path, project)
    assert prepare_combined_edit(tmp_path, corrected).plan.decisions == base.decisions
    save_project(tmp_path, changed)
    assert prepare_combined_edit(tmp_path, corrected).plan.decisions == edit.plan.decisions
    fresh = apply_overrides(changed, base)
    recreated = creation_edit(changed, fresh, edit)
    assert visible(recreated.plan) == visible(edit.plan)
    assert not list(tmp_path.glob("*.mp4"))  # These are metadata-only integration checks.


def test_deleting_every_selected_speed_window_still_keeps_old_gaps_dropped(tmp_path: Path) -> None:
    project, base, video = saved_montage(tmp_path)
    selected = [d for d in base.decisions if d.output_frame_count]
    for decision in selected[1:3]:
        project = edit_episode(project, decision, None)
    save_project(tmp_path, project)
    result = prepare_combined_edit(tmp_path, video).plan
    assert visible(result) == [visible(base)[0], visible(base)[-1]]


def test_explicit_restore_inside_an_inherited_speed_rule_is_still_possible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, base, video = saved_montage(tmp_path)
    grey = next(d for d in base.decisions if d.action == "DROP" and d.source_start_us == 64_000_000)
    project = edit_episode(project, grey, 8)
    assert project.overrides[-1].restore_omitted
    monkeypatch.setattr("sculptcut.restored_work.find_work_windows",
                        lambda *args:[(64_000_000,68_000_000)])
    save_project(tmp_path, project)
    result = prepare_combined_edit(tmp_path, video).plan
    assert visible(result) == sorted([*visible(base), (64_000_000,68_000_000)])
    assert result.actual_frame_count == base.target_frame_count
    assert all(d.output_frame_count >= 15 for d in result.decisions if d.output_frame_count)


def test_legacy_damaged_variant_uses_its_neutral_plan_and_original_speed_rules(
    tmp_path: Path,
) -> None:
    project, base, original = saved_montage(tmp_path)
    project = edit_episode(project, [d for d in base.decisions if d.output_frame_count][-1], None)
    save_project(tmp_path, project)
    damaged = apply_overrides(project, base, manual=True)
    assert len(visible(damaged)) > len(visible(base))
    video = tmp_path / "legacy.mp4"
    save_variant(video, project=project, plan=damaged, parent=original,
                 operation="combined_edit", episode_base_plan=base)
    fixed = prepare_combined_edit(tmp_path, video)
    assert visible(fixed.plan) == visible(base)[:-1]


def test_musical_edit_retimes_cuts_without_reselecting_source_windows() -> None:
    project, plan = motion_plan()
    events = [MusicEvent(t, 1) for t in (1.3, 3.7, 5.2, 7.6, 10.1)]
    plan = retime_episodes(project, plan, events)
    project = edit_episode(project, plan.decisions[2], None)
    result = apply_episode_edits(project, plan)
    assert visible(result) == [span for span in visible(plan) if span != visible(plan)[1]]
    position = aligned = 0
    active = [d for d in result.decisions if d.output_frame_count]
    beats = {round(event.time_seconds * plan.output_fps) for event in events}
    for decision in active[:-1]:
        position += decision.output_frame_count
        aligned += position in beats
    assert result.settings["beat_sync_total_cuts"] == len(active) - 1
    assert result.settings["beat_sync_aligned_cuts"] == aligned >= len(active) - 2
    assert apply_episode_edits(project, result).decisions == result.decisions
