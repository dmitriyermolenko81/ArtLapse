"""Regressions for missing process steps and overlong introductory setup."""

from test_timeline import make_source

from sculptcut.domain import Project, ProjectSettings
from sculptcut.planning import RenderDecision, RenderPlan, validate_plan
from sculptcut.segmentation import Segment
from sculptcut.timelapse_coverage import (
    balance_process_budget,
    limit_setup_context,
    preserve_work_bridges,
)
from sculptcut.timelapse_story import reserve_final_view


def fixture() -> tuple[Project, RenderPlan]:
    project = Project(sources=[make_source(0, 0, 600_000_000)], settings=ProjectSettings())
    parts = [(0, 80, 450, "story:static_episode"),
             (80, 100, 450, "story:static_episode"),
             (100, 400, 0, "story:episode_omission"),
             (400, 600, 900, "story:static_episode")]
    plan = RenderPlan(
        source_fingerprints={"0": project.sources[0].fingerprint},
        output_fps=30, target_frame_count=1800, actual_frame_count=1800,
        settings={"preset": "timelapse", "story": "static_episodes"},
        decisions=[RenderDecision(source_id="0", source_start_us=a*1_000_000,
                                  source_end_us=b*1_000_000,
                                  action="SPEED" if n else "DROP", output_frame_count=n,
                                  provenance=reason) for a, b, n, reason in parts],
    )
    return project, plan


def test_creation_between_highlights_survives_final_view_and_target_budget() -> None:
    project, plan = fixture()
    segments = [Segment(source_id="0", start_us=80_000_000, end_us=600_000_000,
                        kind="SCULPT", score=0.6)]
    result = preserve_work_bridges(project, plan, segments)
    result = reserve_final_view(project, result)
    result = limit_setup_context(project, result, segments)
    result = balance_process_budget(project, result)
    validate_plan(project, result)
    assert result.actual_frame_count == 1800
    bridges = [d for d in result.decisions if d.provenance == "story:process_bridge"]
    assert sum(d.source_end_us-d.source_start_us for d in bridges) == 36_000_000
    assert [d.source_start_us for d in bridges] == [100_000_000, 220_000_000, 340_000_000]
    assert sum(d.output_frame_count for d in bridges) >= 36*30/128
    assert all(d.output_frame_count >= 15 for d in bridges)
    setup = [d for d in result.decisions if d.provenance == "story:setup_context"]
    assert sum(d.output_frame_count for d in setup) == 60
    assert sum(d.source_end_us-d.source_start_us for d in setup) == 2_000_000
    assert any(d.provenance == "story:setup_trim" for d in result.decisions)
    assert all(d.action != "DROP" for d in setup)
    assert any(d.provenance == "user:final_tail" for d in result.decisions)
    # Plans must round-trip without embedding non-scalar data in settings.
    RenderPlan.model_validate_json(result.model_dump_json())


def test_confirmed_inactivity_and_explicit_drops_are_not_restored() -> None:
    project, plan = fixture()
    for kind in ("STATIC", "CAMERA", "INTERFACE", "PROCESSING"):
        segments = [Segment(source_id="0", start_us=100_000_000, end_us=400_000_000,
                            kind=kind, score=1)]
        assert preserve_work_bridges(project, plan, segments) == plan
    work = [Segment(source_id="0", start_us=0, end_us=600_000_000, kind="SCULPT", score=1)]
    for reason in ("user:drop", "idle", "story:timelapse_cleanup", "story:stable_hold"):
        changed = plan.model_copy(update={"decisions": [
            d.model_copy(update={"provenance": reason}) if d.action == "DROP" else d
            for d in plan.decisions]})
        assert preserve_work_bridges(project, changed, work) == changed


def test_uncertain_work_is_preserved_even_among_navigation() -> None:
    project, plan = fixture()
    for kind, score in (("UNKNOWN", 0.2), ("MIXED", 0.5), ("SCULPT", 0.6)):
        segments = [Segment(source_id="0", start_us=100_000_000, end_us=390_000_000,
                            kind="CAMERA", score=1),
                    Segment(source_id="0", start_us=390_000_000, end_us=400_000_000,
                            kind=kind, score=score)]
        result = preserve_work_bridges(project, plan, segments)
        restored = [d for d in result.decisions if d.provenance == "story:process_bridge"]
        assert len(restored) == 1
        assert restored[0].source_start_us == 390_000_000
        assert restored[0].source_end_us == 400_000_000
        assert result.decisions[2].action == "DROP"
        validate_plan(project, result)


def test_weak_static_alone_does_not_resurrect_a_long_gap() -> None:
    project, plan = fixture()
    segments = [Segment(source_id="0", start_us=100_000_000, end_us=400_000_000,
                        kind="STATIC", score=0.5)]
    assert preserve_work_bridges(project, plan, segments) == plan


def test_cleanup_fragments_keep_readable_duration_without_slow_motion() -> None:
    project, plan = fixture()
    parts = [plan.decisions[0], plan.decisions[1]]
    for a, b in ((100, 101), (101, 101.2), (101.2, 400)):
        parts.append(plan.decisions[2].model_copy(update={
            "source_start_us": round(a*1e6), "source_end_us": round(b*1e6),
            "action": "SPEED", "provenance": "story:process_bridge",
            "output_frame_count": 1,
        }))
    parts.append(plan.decisions[3])
    result = reserve_final_view(project, plan.model_copy(update={"decisions": parts}))
    result = limit_setup_context(project, result, [Segment(
        source_id="0", start_us=80_000_000, end_us=90_000_000, kind="SCULPT", score=0.6,
    )])
    result = balance_process_budget(project, result)
    bridges = [d for d in result.decisions if d.provenance == "story:process_bridge"]
    assert bridges[0].output_frame_count >= 15
    assert bridges[1].output_frame_count == 6
    assert result.actual_frame_count == 1800
    validate_plan(project, result)


def test_global_bridge_budget_survives_fragmentation_and_legacy_speed_settings() -> None:
    project, plan = fixture()
    assert project.settings.timelapse_bridge_max_speed == 128
    parts = [*plan.decisions[:2]]
    for start in range(100, 400, 12):
        parts.append(plan.decisions[2].model_copy(update={
            "source_start_us": start*1_000_000, "source_end_us": (start+12)*1_000_000,
            "action": "SPEED", "provenance": "story:process_bridge", "output_frame_count": 1,
        }))
    parts.append(plan.decisions[-1])
    result = balance_process_budget(project, plan.model_copy(update={"decisions": parts}))
    bridges = [d for d in result.decisions if d.provenance == "story:process_bridge"]
    assert len(bridges) == 3
    assert sum(d.output_frame_count for d in bridges) <= 270
    assert all((d.source_end_us-d.source_start_us)*30 <=
               d.output_frame_count*4_000_000 for d in bridges)
    assert result.actual_frame_count == 1800
    assert balance_process_budget(project, result) == result
    validate_plan(project, result)


def test_fractional_source_duration_does_not_exceed_bridge_speed_limit() -> None:
    project, plan = fixture()
    bridge = plan.decisions[2].model_copy(update={
        "source_end_us": 102_010_000, "action": "SPEED",
        "provenance": "story:process_bridge", "output_frame_count": 1,
    })
    tail = plan.decisions[2].model_copy(update={"source_start_us": 102_010_000})
    result = balance_process_budget(project, plan.model_copy(update={
        "decisions": [*plan.decisions[:2], bridge, tail, plan.decisions[-1]],
    }))
    selected = next(d for d in result.decisions if d.provenance == "story:process_bridge")
    assert selected.output_frame_count == 16
    assert 2.01/(selected.output_frame_count/30) <= 4
    validate_plan(project, result)


def test_bridge_budget_and_work_preference_do_not_expand_to_whole_gap() -> None:
    project, plan = fixture()
    segments = [Segment(source_id="0", start_us=100_000_000, end_us=200_000_000,
                        kind="STATIC", score=0.5),
                Segment(source_id="0", start_us=200_000_000, end_us=204_000_000,
                        kind="SCULPT", score=0.6),
                Segment(source_id="0", start_us=204_000_000, end_us=400_000_000,
                        kind="STATIC", score=0.5)]
    result = preserve_work_bridges(project, plan, segments)
    restored = [d for d in result.decisions if d.provenance == "story:process_bridge"]
    assert len(restored) == 1
    assert restored[0].source_start_us == 200_000_000
    assert restored[0].source_end_us == 204_000_000
    assert result == preserve_work_bridges(project, plan, list(reversed(segments)))
    validate_plan(project, result)


def test_setup_requires_sustained_early_work_and_respects_manual_keep() -> None:
    project, plan = fixture()
    for start, end in ((0, 600), (80, 81), (150, 160)):
        segments = [Segment(source_id="0", start_us=start*1_000_000,
                            end_us=end*1_000_000, kind="SCULPT", score=0.6)]
        assert limit_setup_context(project, plan, segments) == plan
    segments = [Segment(source_id="0", start_us=80_000_000, end_us=90_000_000,
                        kind="SCULPT", score=0.6)]
    changed = plan.model_copy(update={"decisions": [
        plan.decisions[0].model_copy(update={"provenance": "user:keep"}), *plan.decisions[1:]]})
    assert limit_setup_context(project, changed, segments) == changed
