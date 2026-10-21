"""Chronological episode selection and real short-video export."""

from pathlib import Path

import pytest
from test_timeline import make_source

from sculptcut.domain import Project, ProjectSettings
from sculptcut.idle import IdleRange
from sculptcut.planning import RenderPlan, build_plan, validate_plan
from sculptcut.segmentation import Segment
from sculptcut.timelapse_story import episodic_timelapse_plan, reserve_final_view


def test_episodes_cover_sources_with_explicit_cuts_and_fixed_camera() -> None:
    first = make_source(0, 0, 1_800_000_000)
    second = make_source(1, first.duration_us, 1_800_000_000)
    project = Project(
        sources=[first, second],
        settings=ProjectSettings(preset="timelapse", output_fps=30),
    )
    plan = episodic_timelapse_plan(project, build_plan(project, [], 600), [])
    validate_plan(project, plan)
    assert plan.actual_frame_count == 18_000
    assert plan.settings["episode_count"] == 75
    assert sum(d.action != "DROP" for d in plan.decisions) == 75
    assert any(d.provenance == "story:episode_omission" for d in plan.decisions)
    assert all(d.subject_camera is None for d in plan.decisions)
    assert all(d.transition == "cut" for d in plan.decisions)
    assert (
        max(
            (d.source_end_us - d.source_start_us) * 30 / (d.output_frame_count * 1_000_000)
            for d in plan.decisions
            if d.action != "DROP"
        )
        <= 8.01
    )


def test_episode_prefers_work_to_navigation() -> None:
    project = Project(
        sources=[make_source(0, 0, 90_000_000)],
        settings=ProjectSettings(preset="timelapse", output_fps=30),
    )
    segments = [
        Segment(source_id="0", start_us=0, end_us=30_000_000, kind="CAMERA", score=1),
        Segment(source_id="0", start_us=60_000_000, end_us=90_000_000, kind="SCULPT", score=1),
    ]
    plan = episodic_timelapse_plan(project, build_plan(project, [], 8), segments)
    kept = [d for d in plan.decisions if d.action != "DROP"]
    assert len(kept) == 1
    assert (kept[0].source_start_us, kept[0].source_end_us) == (60_000_000, 90_000_000)


def test_long_timelapse_preserves_more_early_blocking_stages() -> None:
    source = make_source(0, 0, 13_061_600_000)
    segments = [
        Segment(source_id="0", start_us=0, end_us=600_000_000, kind="SCULPT", score=1),
        Segment(
            source_id="0",
            start_us=600_000_000,
            end_us=source.duration_us,
            kind="MIXED",
            score=0.5,
        ),
    ]
    focused = Project(sources=[source], settings=ProjectSettings(preset="timelapse"))
    linear = focused.model_copy(
        update={
            "settings": focused.settings.model_copy(update={"timelapse_early_chapter_power": 1})
        }
    )
    focused_plan = episodic_timelapse_plan(focused, build_plan(focused, [], 240), segments)
    linear_plan = episodic_timelapse_plan(linear, build_plan(linear, [], 240), segments)

    def early_episodes(plan: RenderPlan) -> list[int]:
        return [
            decision.source_start_us
            for decision in plan.decisions
            if decision.action != "DROP"
            and decision.source_start_us < 600_000_000
            and decision.provenance == "story:static_episode"
        ]

    focused_early = early_episodes(focused_plan)
    assert focused_early[0] == 0
    assert len(focused_early) >= 5
    assert len(focused_early) > len(early_episodes(linear_plan))
    assert focused_plan.actual_frame_count == linear_plan.actual_frame_count == 240 * 30


def test_episode_starts_before_sustained_work_instead_of_arbitrary_grid() -> None:
    project = Project(
        sources=[make_source(0, 0, 60_000_000)],
        settings=ProjectSettings(preset="timelapse", output_fps=30),
    )
    segments = [
        Segment(source_id="0", start_us=0, end_us=11_000_000, kind="UNKNOWN", score=0),
        Segment(source_id="0", start_us=11_000_000, end_us=31_000_000, kind="SCULPT", score=1),
        Segment(source_id="0", start_us=31_000_000, end_us=60_000_000, kind="UNKNOWN", score=0),
    ]
    plan = episodic_timelapse_plan(project, build_plan(project, [], 8), segments)
    kept = [d for d in plan.decisions if d.action != "DROP"]
    assert [(d.source_start_us, d.source_end_us) for d in kept] == [(10_000_000, 40_000_000)]


def test_static_chapters_do_not_fill_the_target_duration() -> None:
    project = Project(
        sources=[make_source(0, 0, 180_000_000)],
        settings=ProjectSettings(preset="timelapse", output_fps=30),
    )
    segments = [
        Segment(source_id="0", start_us=0, end_us=60_000_000, kind="SCULPT", score=1),
        Segment(source_id="0", start_us=60_000_000, end_us=120_000_000, kind="STATIC", score=1),
        Segment(source_id="0", start_us=120_000_000, end_us=180_000_000, kind="SCULPT", score=1),
    ]
    plan = episodic_timelapse_plan(project, build_plan(project, [], 48), segments)
    validate_plan(project, plan)
    assert plan.settings["episode_count"] == 4
    assert plan.actual_frame_count == 48 * 30
    assert all(
        decision.action == "DROP"
        for decision in plan.decisions
        if decision.source_start_us >= 60_000_000 and decision.source_end_us <= 120_000_000
    )


def test_sustained_static_hold_is_cut_inside_selected_work_window() -> None:
    project = Project(
        sources=[make_source(0, 0, 90_000_000)],
        settings=ProjectSettings(preset="timelapse", output_fps=30),
    )
    segments = [
        Segment(source_id="0", start_us=0, end_us=20_000_000, kind="SCULPT", score=1),
        Segment(source_id="0", start_us=20_000_000, end_us=25_000_000, kind="STATIC", score=1),
        Segment(source_id="0", start_us=25_000_000, end_us=90_000_000, kind="SCULPT", score=1),
    ]
    plan = episodic_timelapse_plan(project, build_plan(project, [], 16), segments)
    validate_plan(project, plan)
    assert any(
        d.source_start_us == 20_000_000
        and d.source_end_us == 20_600_000
        and d.action == "KEEP"
        and d.output_frame_count == 18
        and d.provenance == "story:outcome_hold"
        for d in plan.decisions
    )
    assert any(
        d.source_start_us == 20_600_000
        and d.source_end_us == 25_000_000
        and d.action == "DROP"
        and d.provenance == "story:stable_hold"
        for d in plan.decisions
    )
    final = reserve_final_view(project, plan)
    assert any(
        d.provenance == "story:outcome_hold" and d.output_frame_count == 18 for d in final.decisions
    )


def test_short_work_span_beats_long_static_span_in_same_chapter() -> None:
    project = Project(
        sources=[make_source(0, 0, 60_000_000)],
        settings=ProjectSettings(preset="timelapse", output_fps=30),
    )
    base = build_plan(
        project,
        [IdleRange(source_id="0", start_us=50_000_000, end_us=51_000_000)],
        8,
    )
    segments = [
        Segment(source_id="0", start_us=0, end_us=50_000_000, kind="STATIC", score=1),
        Segment(source_id="0", start_us=51_000_000, end_us=60_000_000, kind="SCULPT", score=1),
    ]
    plan = episodic_timelapse_plan(project, base, segments)
    validate_plan(project, plan)
    assert [(d.source_start_us, d.source_end_us) for d in plan.decisions if d.action != "DROP"] == [
        (51_000_000, 60_000_000)
    ]


def test_uncertain_static_labels_preserve_episode_budget_and_acceleration() -> None:
    project = Project(
        sources=[make_source(0, 0, 1_200_000_000)],
        settings=ProjectSettings(preset="timelapse", output_fps=30),
    )
    # Smoothed STATIC is a low-change hypothesis, not confirmed inactivity.
    segments = [Segment(source_id="0", start_us=0, end_us=1_200_000_000,
                        kind="STATIC", score=0.5)]
    plan = episodic_timelapse_plan(project, build_plan(project, [], 300), segments)
    final = reserve_final_view(project, plan)
    validate_plan(project, final)
    assert plan.settings["episode_count"] == 38
    assert final.actual_frame_count == 300 * 30
    episodes = [d for d in final.decisions if d.provenance == "story:static_episode"]
    assert len(episodes) >= 38
    assert all((d.source_end_us-d.source_start_us) * final.output_fps
               > d.output_frame_count * 2_000_000 for d in episodes)
    assert not any(d.provenance == "story:stable_hold" for d in final.decisions)


def test_fully_static_source_has_no_useful_episode() -> None:
    project = Project(
        sources=[make_source(0, 0, 60_000_000)],
        settings=ProjectSettings(preset="timelapse", output_fps=30),
    )
    static = [Segment(source_id="0", start_us=0, end_us=60_000_000, kind="STATIC", score=1)]
    with pytest.raises(ValueError, match="No useful timelapse episodes"):
        episodic_timelapse_plan(project, build_plan(project, [], 8), static)


def test_final_tail_does_not_restore_idle_at_normal_speed() -> None:
    first = make_source(0, 0, 1_800_000_000)
    last = make_source(1, first.duration_us, 1_800_000_000)
    project = Project(
        sources=[first, last],
        settings=ProjectSettings(preset="timelapse", output_fps=30),
    )
    idle = [
        IdleRange(
            source_id=last.id,
            start_us=last.duration_us - 20_000_000,
            end_us=last.duration_us,
            reason="static",
        )
    ]
    base = episodic_timelapse_plan(project, build_plan(project, idle, 600), [])
    last_useful = [d for d in base.decisions if d.action != "DROP"][-1].source_end_us
    plan = reserve_final_view(project, base)
    validate_plan(project, plan)
    final = [d for d in plan.decisions if d.action != "DROP"][-1]
    assert final.source_id == last.id
    assert final.source_start_us == last_useful - 2_000_000
    assert final.source_end_us == last_useful
    assert final.source_end_us <= last.duration_us - 20_000_000
    assert all(d.action == "DROP" for d in plan.decisions
               if d.source_id == last.id and d.source_start_us >= last.duration_us - 20_000_000)
    assert final.action == "KEEP"
    assert final.provenance == "user:final_tail"
    assert final.output_frame_count == 60
    assert plan.actual_frame_count == 18_000


def test_final_ends_before_removed_screen() -> None:
    source = make_source(0, 0, 90_000_000)
    project = Project(
        sources=[source],
        settings=ProjectSettings(preset="timelapse", output_fps=30),
    )
    removed = [
        IdleRange(
            source_id=source.id,
            start_us=85_000_000,
            end_us=90_000_000,
            reason="user:remove_screen",
        )
    ]
    base = episodic_timelapse_plan(project, build_plan(project, removed, 30), [])
    last_useful = [d for d in base.decisions if d.action != "DROP"][-1].source_end_us
    plan = reserve_final_view(project, base)
    final = [d for d in plan.decisions if d.action != "DROP"][-1]
    assert final.provenance == "user:final_tail"
    assert final.source_end_us == last_useful
    assert final.source_end_us <= 85_000_000
    assert (
        final.output_frame_count
        == (final.source_end_us - final.source_start_us) * plan.output_fps // 1_000_000
    )


def test_episodic_timelapse_exports_decodable_video(tmp_path: Path) -> None:
    from sculptcut.analysis import extract_features
    from sculptcut.media import discover, run_process
    from sculptcut.planning import plan_project
    from sculptcut.project import create_project
    from sculptcut.proxy import make_proxies
    from sculptcut.rendering import render, verify_output

    video = tmp_path / "drawing.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=160x90:r=10:d=75",
            "-c:v",
            "libx264",
            str(video),
        ]
    )
    root = tmp_path / "project"
    project = create_project(root, [video]).model_copy(
        update={"settings": ProjectSettings(preset="timelapse", output_fps=10)}
    )
    make_proxies(root, project)
    extract_features(root, project)
    plan = plan_project(root, project, 16)
    assert plan.settings["story"] == "static_episodes"
    assert len([d for d in plan.decisions if d.action != "DROP"]) >= 2
    # Bridges represent gaps within a global screen budget, not at extreme speed.
    assert any(d.provenance == "story:process_bridge" for d in plan.decisions)
    bridges = [d for d in plan.decisions if d.provenance == "story:process_bridge"]
    assert sum(d.output_frame_count for d in bridges) <= 24
    assert all((d.source_end_us-d.source_start_us)*10 <=
               d.output_frame_count*4_000_000 for d in bridges)
    final = [d for d in plan.decisions if d.action != "DROP"][-1]
    assert final.provenance == "user:final_tail"
    assert final.action == "HOLD"
    assert final.output_frame_count >= (final.source_end_us-final.source_start_us) * 10//1_000_000
    assert plan.settings["bookends"]
    output = render(root, project, plan, tmp_path / "episodes.mp4", encoder="libx264")
    verify_output(output, 160, 10)
