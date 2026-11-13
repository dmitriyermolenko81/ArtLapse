"""Saved speed edits must not consume the timeline by resurrecting musical omissions."""

from test_timeline import make_source

from sculptcut.domain import Override, Project, ProjectSettings
from sculptcut.overrides import apply_overrides
from sculptcut.planning import RenderDecision, build_plan, validate_plan


def musical_plan(target: float = 8):
    project = Project(sources=[make_source(0, 0, 240_000_000)], settings=ProjectSettings(
        preset="music_shorts", target_seconds=target,
    ))
    decisions = []
    cursor = 0
    for start in (0, 60, 120, 200):
        if cursor < start:
            decisions.append(RenderDecision(source_id="0", source_start_us=cursor * 1_000_000,
                source_end_us=start * 1_000_000, action="DROP", output_frame_count=0,
                provenance="user:process_omission"))
        decisions.append(RenderDecision(source_id="0", source_start_us=start * 1_000_000,
            source_end_us=(start + 4) * 1_000_000, action="SPEED", output_frame_count=60,
            provenance="user:process_story"))
        cursor = start + 4
    decisions.append(RenderDecision(source_id="0", source_start_us=cursor * 1_000_000,
        source_end_us=240_000_000, action="DROP", output_frame_count=0,
        provenance="user:process_omission"))
    base = build_plan(project, [], target)
    plan = base.model_copy(update={"decisions": decisions, "actual_frame_count": 240,
                                   "settings": base.settings | {"story": "process"}})
    return project, plan


def test_automatic_music_speed_edit_only_affects_selected_windows() -> None:
    project, plan = musical_plan()
    project = project.model_copy(update={"overrides": [Override(
        source_id="0", start_us=20_000_000, end_us=160_000_000, action="SET_SPEED", speed=8,
    )]})
    result = apply_overrides(project, plan)
    validate_plan(project, result)
    assert result.actual_frame_count == 240
    active = [d for d in result.decisions if d.output_frame_count]
    assert [(d.source_start_us, d.source_end_us) for d in active] == [
        (0, 4_000_000), (60_000_000, 64_000_000),
        (120_000_000, 124_000_000), (200_000_000, 204_000_000),
    ]
    locked = [d for d in active if d.provenance == "user:SET_SPEED"]
    assert len(locked) == 2 and all(d.output_frame_count == 15 for d in locked)
    assert all(d.output_frame_count >= 12 for d in active)
    # A direct edit of a grey episode deliberately restores its selected interval.
    explicit = apply_overrides(project, plan, manual=True)
    assert any(d.output_frame_count and d.source_start_us == 20_000_000
               and d.source_end_us == 60_000_000 for d in explicit.decisions)


def test_music_speed_constraints_do_not_squeeze_other_shots_to_microcuts() -> None:
    project, plan = musical_plan(2)
    project = project.model_copy(update={"overrides": [Override(
        source_id="0", start_us=60_000_000, end_us=64_000_000, action="SET_SPEED", speed=2,
    )]})
    result = apply_overrides(project, plan)
    assert result.actual_frame_count > plan.target_frame_count
    active = [d for d in result.decisions if d.output_frame_count]
    assert all(d.output_frame_count >= 15 for d in active)
    assert result.warnings  # A real constraint conflict is reported, never hidden by 32x shots.
