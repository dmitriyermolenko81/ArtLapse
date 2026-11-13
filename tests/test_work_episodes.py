"""Full-source candidates, readable short actions and safe refill after edits."""

from collections import defaultdict
from pathlib import Path

import numpy as np
from test_timeline import make_source

from sculptcut.analysis import FrameFeature
from sculptcut.bookends import present_final_result
from sculptcut.development import DevelopmentEvidence
from sculptcut.domain import Override, Project, ProjectSettings
from sculptcut.overrides import apply_overrides, exclude_dropped
from sculptcut.planning import build_plan, validate_plan
from sculptcut.project import save_project
from sculptcut.segmentation import Segment
from sculptcut.shorts_refill import refill_plan_after_cuts
from sculptcut.timelapse_phrases import smooth_phrases
from sculptcut.work_episodes import detect_episodes, highlight_plan, refill_highlights


def work(start, end, kind="SCULPT", score=0.9):
    return Segment(source_id="0", start_us=start * 1_000_000, end_us=end * 1_000_000,
                   kind=kind, score=score, importance=0.8)


def test_entire_source_is_detected_before_duration_or_legacy_episode_limit() -> None:
    project = Project(sources=[make_source(0, 0, 7200_000_000)],
                      settings=ProjectSettings(timelapse_episode_limit=1), overrides=[
                          Override(source_id="0", start_us=3600_000_000,
                                   end_us=3610_000_000, action="DROP"),
                      ])
    segments = [work(0, 7200)]
    short_base = exclude_dropped(project, build_plan(project, [], 30))
    long_base = exclude_dropped(project, build_plan(project, [], 600))
    pool = detect_episodes(project, short_base, segments)
    assert pool == detect_episodes(project, long_base, segments)
    assert len(pool) > 1000
    assert sum(e.end_us - e.start_us for e in pool) == 7190_000_000
    assert all(e.end_us <= 3600_000_000 or e.start_us >= 3610_000_000 for e in pool)
    assert all(e.end_us - e.start_us <= 6_000_000 for e in pool)
    plan = highlight_plan(project, long_base, pool)
    validate_plan(project, plan)
    active = [d for d in plan.decisions if d.output_frame_count]
    assert len({d.episode_id for d in active}) > 300
    assert plan.actual_frame_count == 18000
    assert max(d.output_frame_count for d in active) <= 90
    assert max(d.source_end_us for d in active) >= 7194_000_000


def test_quiet_is_removed_but_small_detail_and_unmeasured_work_remain_candidates() -> None:
    project = Project(sources=[make_source(0, 0, 30_000_000)])
    features = [FrameFeature(
        source_id="0", timestamp_us=i * 1_000_000, brightness=0.4, contrast=0.1,
        roi_change=0.0001 if 6 <= i < 12 else 0,
        ui_change=0, edge_change=0.0001 if 6 <= i < 12 else 0,
        histogram_change=0, changed_pixel_ratio=0.0002 if 6 <= i < 12 else 0,
        noise=0, perceptual_hash="0", measurement_quality=1,
    ) for i in range(18)]
    base = build_plan(project, [], 10)
    pool = detect_episodes(project, base, [work(0, 30)], iter(features))
    assert not any(e.start_us < 6_000_000 for e in pool)
    assert any(e.start_us == 6_000_000 for e in pool)
    assert not any(e.start_us == 12_000_000 for e in pool)
    assert any(e.start_us >= 18_000_000 and e.samples == 0 for e in pool)
    paused = detect_episodes(project, base, [
        work(0, 6), work(6, 18, "STATIC"), work(18, 30, "UNKNOWN", 0.2),
    ])
    assert all(e.end_us <= 6_000_000 or e.start_us >= 18_000_000 for e in paused)
    assert any(e.kind == "UNKNOWN" for e in paused)


def test_observed_progress_wins_over_repetition_and_one_x_limit_trims_source() -> None:
    project = Project(sources=[make_source(0, 0, 600_000_000)], settings=ProjectSettings(
        timelapse_episode_max_speed=1,
    ))
    base = build_plan(project, [], 30)
    pool = detect_episodes(project, base, [work(0, 600, "UNKNOWN", 0.2)])
    state = np.tile(np.arange(128, dtype=np.uint8), (128, 1))

    def evidence(sid, a, b):
        return DevelopmentEvidence(1 if (a // 6_000_000) % 2 else 0, state)

    plan = smooth_phrases(highlight_plan(project, base, pool, evidence))
    active = [d for d in plan.decisions if d.output_frame_count]
    progressing = sum(int(d.episode_id.split(":")[-2]) // 6_000_000 % 2 for d in active)
    assert progressing > len(active) * 0.7
    assert plan.actual_frame_count == 900
    assert all((d.source_end_us - d.source_start_us) * 30 <= d.output_frame_count * 1e6 + 30
               for d in active)
    assert all(d.speed_ramp is None for d in active)


def test_episode_removals_and_output_cuts_refill_with_other_short_actions() -> None:
    project = Project(sources=[make_source(0, 0, 1200_000_000)],
                      settings=ProjectSettings(target_seconds=60, final_view_seconds=0))
    base = build_plan(project, [], 60)
    plan = smooth_phrases(highlight_plan(project, base, detect_episodes(
        project, base, [work(0, 1200)]
    )))
    active = [d for d in plan.decisions if d.output_frame_count]
    original_ids = {d.episode_id for d in active}
    removed = active[:len(active) // 2]
    project = project.model_copy(update={"overrides": [Override(
        source_id=d.source_id, start_us=d.source_start_us, end_us=d.source_end_us, action="DROP"
    ) for d in removed]})
    edited = refill_highlights(project, apply_overrides(project, plan, manual=True))
    validate_plan(project, edited)
    assert edited.actual_frame_count == 1800
    assert edited.settings["work_needs_quality_check"] == 1
    new_ids = {d.episode_id for d in edited.decisions if d.output_frame_count}
    assert len(new_ids) >= len(original_ids)
    assert {d.episode_id for d in edited.decisions if d.output_frame_count} - original_ids
    assert all(not d.output_frame_count or all(
        d.source_end_us <= r.source_start_us or d.source_start_us >= r.source_end_us
        for r in removed
    ) for d in edited.decisions)
    edited = smooth_phrases(edited)
    trimmed = refill_plan_after_cuts(project, edited, [(1_000_000, 2_000_000)])
    validate_plan(project, trimmed)
    assert trimmed.actual_frame_count == 1800
    counts = defaultdict(int)
    for d in trimmed.decisions:
        if d.episode_id:
            counts[d.episode_id] += d.output_frame_count
    assert max(counts.values()) <= 90
    cuts = [d for d in trimmed.decisions if d.provenance == "user:shorts_manual_cut"]
    assert cuts and all(d.action == "DROP" for d in cuts)


def test_editor_keeps_unselected_work_episodes_separate(tmp_path: Path) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from sculptcut.episode_editor import EpisodeEditor

    app = QApplication.instance() or QApplication([])
    project = Project(sources=[make_source(0, 0, 600_000_000)], settings=ProjectSettings(
        target_seconds=20, final_view_seconds=0,
    ))
    base = build_plan(project, [], 20)
    pool = detect_episodes(project, base, [work(0, 600)])
    plan = highlight_plan(project, base, pool)
    save_project(tmp_path, project)
    (tmp_path / "render_plan.json").write_text(plan.model_dump_json(), encoding="utf-8")
    editor = EpisodeEditor()
    try:
        editor.load(tmp_path)
        editor.show()
        app.processEvents()
        displayed = [editor.item(i).data(Qt.ItemDataRole.UserRole) for i in range(editor.count())]
        assert editor.isVisible()
        assert {d.episode_id for d in displayed} == {e.id for e in pool}
        omitted = [d for d in displayed if d.action == "DROP"]
        assert len(omitted) > 60
        assert max(d.source_end_us - d.source_start_us for d in omitted) <= 6_000_000
    finally:
        editor.close()


def test_final_still_does_not_replace_the_last_short_work_action() -> None:
    project = Project(sources=[make_source(0, 0, 1200_000_000)], settings=ProjectSettings(
        target_seconds=60, final_view_seconds=4,
    ))
    base = build_plan(project, [], 60)
    plan = highlight_plan(project, base, detect_episodes(project, base, [work(0, 1200)]))
    last = next(d for d in reversed(plan.decisions) if d.output_frame_count)
    result = refill_highlights(project, present_final_result(project, plan))
    validate_plan(project, result)
    assert result.actual_frame_count == 1800
    assert any(d.output_frame_count and d.episode_id == last.episode_id and d.action == "SPEED"
               for d in result.decisions)
    final = [d for d in result.decisions if d.action == "HOLD"]
    assert len(final) == 1 and final[0].output_frame_count == 120


def test_small_source_can_use_real_time_instead_of_shortening_or_filling() -> None:
    project = Project(sources=[make_source(0, 0, 60_000_000)], settings=ProjectSettings(
        target_seconds=60, final_view_seconds=0,
    ))
    base = build_plan(project, [], 60)
    plan = highlight_plan(project, base, detect_episodes(project, base, [work(0, 60)]))
    plan = smooth_phrases(refill_highlights(project, plan))
    validate_plan(project, plan)
    assert plan.actual_frame_count == 1800
    assert sum(d.source_end_us - d.source_start_us for d in plan.decisions
               if d.output_frame_count) == 60_000_000
    assert all(d.action == "SPEED" and d.speed_ramp is None for d in plan.decisions)


def test_navigation_is_not_work_but_mixed_motion_and_edits_stay_candidates() -> None:
    project = Project(sources=[make_source(0, 0, 30_000_000)])
    base = build_plan(project, [], 10)
    pool = detect_episodes(project, base, [
        work(0, 10, "CAMERA", 0.8), work(10, 20, "MIXED", 0.7), work(20, 30),
    ])
    assert pool and all(e.start_us >= 10_000_000 for e in pool)
    assert any(e.start_us < 20_000_000 for e in pool)


def test_highlight_cleanup_removes_confirmed_navigation(monkeypatch) -> None:
    from sculptcut.idle import IdleRange
    from sculptcut.timelapse_cleanup import clean_timelapse

    project = Project(sources=[make_source(0, 0, 6_000_000)])
    plan = build_plan(project, [], 2)
    plan = plan.model_copy(update={"settings": plan.settings | {
        "story": "phrased_timelapse", "work_episodes_version": "261",
    }})
    monkeypatch.setattr("sculptcut.timelapse_cleanup.cleanup_ranges", lambda *args: [
        IdleRange(source_id="0", start_us=0, end_us=2_000_000,
                  reason="timelapse_camera_only"),
        IdleRange(source_id="0", start_us=2_000_000, end_us=3_000_000,
                  reason="timelapse_static_view"),
    ])
    result = clean_timelapse(Path("unused"), project, plan)
    assert all(not d.output_frame_count or d.source_start_us >= 3_000_000
               for d in result.decisions)


def test_unverified_candidates_and_unverified_parts_cannot_refill_the_movie() -> None:
    project = Project(sources=[make_source(0, 0, 120_000_000)])
    base = build_plan(project, [], 12)
    pool = detect_episodes(project, base, [work(0, 120)])
    pool = [e.model_copy(update={
        "process_verified": i % 2 == 1,
        "work_start_us": e.start_us + 1_000_000 if i % 2 else None,
        "work_end_us": e.end_us - 1_000_000 if i % 2 else None,
    }) for i, e in enumerate(pool)]
    plan = highlight_plan(project, base, pool)
    forbidden = [d for d in plan.decisions if d.provenance == "story:unverified_work"]
    assert forbidden
    plan = refill_highlights(project, plan)
    validate_plan(project, plan)
    assert plan.actual_frame_count == 360
    assert all(not d.output_frame_count or all(
        d.source_end_us <= f.source_start_us or d.source_start_us >= f.source_end_us
        for f in forbidden
    ) for d in plan.decisions)


def test_no_confirmed_work_never_falls_back_to_the_full_moving_source() -> None:
    project = Project(sources=[make_source(0, 0, 60_000_000)])
    base = build_plan(project, [], 30)
    pool = [e.model_copy(update={"process_verified": False})
            for e in detect_episodes(project, base, [work(0, 60)])]
    result = highlight_plan(project, base, pool)
    validate_plan(project, result)
    assert result.actual_frame_count == 1
    assert result.settings["work_verified_count"] == 0


def test_process_cache_retains_each_window(tmp_path: Path) -> None:
    from sculptcut.work_episodes import process_score

    project = Project(sources=[make_source(0, 0, 60_000_000)])
    calls = []

    def score(sid, start, end):
        calls.append(start)
        return DevelopmentEvidence(0.8 if start == 0 else None)

    assert process_score(tmp_path, project, score, "0", 0, 3_000_000) == 0.8
    assert process_score(tmp_path, project, score, "0", 6_000_000, 9_000_000) is None
    assert process_score(tmp_path, project, score, "0", 0, 3_000_000) == 0.8
    assert calls == [0, 6_000_000]

    drawing = project.model_copy(update={"settings": project.settings.model_copy(
        update={"preset": "photoshop_shorts"})})
    assert process_score(tmp_path, drawing, lambda *args: DevelopmentEvidence(0.1),
                         "0", 0, 3_000_000) == 0.1
    # Switching material must preserve each material's cached evidence.
    assert process_score(tmp_path, project, score, "0", 0, 3_000_000) == 0.8
    assert calls == [0, 6_000_000]


def test_complete_catalogue_is_checked_and_best_work_window_is_used(tmp_path, monkeypatch) -> None:
    import json

    from sculptcut.work_episodes import project_highlights

    project = Project(sources=[make_source(0, 0, 360_000_000)])
    base = build_plan(project, [], 10)
    calls = []

    def score(sid, a, b):
        calls.append((a, b))
        return DevelopmentEvidence(0.9 if a % 6_000_000 == 2_640_000 else 0.2)

    stages = []
    plan = project_highlights(tmp_path, project, base, [work(0, 360)], score, None, stages.append)
    catalogue = json.loads((tmp_path / "work_episodes.json").read_text())["candidates"]
    assert len(catalogue) == 60 and len(calls) == 180
    assert all(e["process_checked"] and e["process_verified"] for e in catalogue)
    assert all(e["process_strength"] == 0.9 for e in catalogue)
    assert all(e["work_start_us"] == e["end_us"] - 3_360_000 for e in catalogue)
    assert plan.actual_frame_count == 300
    assert "60 / 60" in stages[-1]
    # A shorter export changes the selection, not which work was analysed.
    shorter = project_highlights(tmp_path, project, build_plan(project, [], 3),
                                 [work(0, 360)], score, None)
    again = json.loads((tmp_path / "work_episodes.json").read_text())["candidates"]
    assert len(calls) == 180  # all window evidence reused from the persistent cache
    assert [e["work_start_us"] for e in again] == [e["work_start_us"] for e in catalogue]
    assert shorter.actual_frame_count == 90
