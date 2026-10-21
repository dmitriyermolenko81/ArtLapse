from pathlib import Path

import numpy as np
import pytest
from test_timeline import make_source

from sculptcut.domain import Project, ProjectSettings
from sculptcut.planning import RenderDecision, RenderPlan, build_plan, validate_plan
from sculptcut.process_story import (
    STORY,
    readable_episodes,
    work_chapters,
)
from sculptcut.segmentation import Segment


def test_minute_budget_with_long_pause(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sculptcut.process_story import process_plan

    monkeypatch.setattr("sculptcut.process_story.project_screens", lambda *args: [])
    monkeypatch.setattr("sculptcut.shorts_quality.EpisodeInspector.score", lambda *args: 1.0)
    monkeypatch.setattr("sculptcut.shorts_quality.EpisodeInspector.signature", lambda *args: None)
    monkeypatch.setattr("sculptcut.sculpt_only.final_start", lambda *args: 12_996_000_000)
    project = Project(
        sources=[make_source(0, 0, 13_000_000_000)], settings=ProjectSettings(preset="shorts")
    )
    segments = [
        Segment(source_id="0", start_us=0, end_us=600_000_000, kind="SCULPT", score=1),
        Segment(source_id="0", start_us=600_000_000, end_us=9_600_000_000, kind="STATIC", score=1),
        Segment(
            source_id="0", start_us=9_600_000_000, end_us=13_000_000_000, kind="SCULPT", score=1
        ),
    ]
    plan = process_plan(tmp_path, project, build_plan(project, [], 60), segments)
    validate_plan(project, plan)
    assert plan.actual_frame_count == 1800
    assert plan.settings["bookends"] is True
    finals = [d for d in plan.decisions if d.provenance == "user:shorts_final"]
    assert finals
    assert plan.settings["final_view_frames"] == sum(d.output_frame_count for d in finals)
    assert all(
        d.output_frame_count == (d.source_end_us - d.source_start_us) * plan.output_fps // 1_000_000
        for d in finals
    )
    kept = [d for d in plan.decisions if d.provenance == "user:process_story"]
    assert len(kept) == STORY.chapters
    for d in kept:
        assert d.source_end_us <= 600_000_000 or d.source_start_us >= 9_600_000_000
        assert (d.source_end_us - d.source_start_us) * 30 <= d.output_frame_count * 8_000_000


def test_shorts_spreads_short_work_episodes_across_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sculptcut.process_story import process_plan

    monkeypatch.setattr("sculptcut.process_story.project_screens", lambda *args: [])
    monkeypatch.setattr("sculptcut.shorts_quality.EpisodeInspector.score", lambda *args: 1.0)
    monkeypatch.setattr("sculptcut.shorts_quality.EpisodeInspector.signature", lambda *args: None)
    monkeypatch.setattr("sculptcut.sculpt_only.final_start", lambda *args: None)
    project = Project(
        sources=[make_source(0, 0, 600_000_000)],
        settings=ProjectSettings(preset="shorts"),
    )
    segments = [Segment(source_id="0", start_us=0, end_us=600_000_000, kind="SCULPT", score=1)]
    plan = process_plan(tmp_path, project, build_plan(project, [], 60), segments)
    episodes = [d for d in plan.decisions if d.provenance == "user:process_story"]
    assert len(episodes) == STORY.chapters
    assert all(
        abs(d.source_start_us - round(600_000_000 * i / STORY.chapters)) <= 1
        for i, d in enumerate(episodes)
    )
    assert all(d.source_end_us - d.source_start_us == STORY.episode_us for d in episodes)
    assert all(d.output_frame_count == 40 for d in episodes)
    assert plan.actual_frame_count == 1800


def test_long_shorts_preserve_more_early_blocking_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sculptcut.process_story import process_plan

    monkeypatch.setattr("sculptcut.process_story.project_screens", lambda *args: [])
    monkeypatch.setattr("sculptcut.shorts_quality.EpisodeInspector.score", lambda *args: 1.0)
    monkeypatch.setattr("sculptcut.shorts_quality.EpisodeInspector.signature", lambda *args: None)
    monkeypatch.setattr("sculptcut.sculpt_only.final_start", lambda *args: None)
    source = make_source(0, 0, 9_000_000_000)
    segment = Segment(source_id="0", start_us=0, end_us=source.duration_us, kind="SCULPT", score=1)
    focused = Project(sources=[source], settings=ProjectSettings(preset="shorts"))
    linear = focused.model_copy(
        update={"settings": focused.settings.model_copy(update={"shorts_early_chapter_power": 1})}
    )
    focused_plan = process_plan(tmp_path, focused, build_plan(focused, [], 90), [segment])
    linear_plan = process_plan(tmp_path, linear, build_plan(linear, [], 90), [segment])

    def early_count(plan: RenderPlan) -> int:
        return sum(
            decision.provenance == "user:process_story" and decision.source_start_us < 600_000_000
            for decision in plan.decisions
        )

    assert early_count(focused_plan) >= 8
    assert early_count(focused_plan) > early_count(linear_plan)
    assert focused_plan.actual_frame_count == linear_plan.actual_frame_count == 90 * 30


def test_work_coverage_survives_long_pause_and_source_change() -> None:
    def decision(source: str, start: int, end: int, drop: bool = False) -> RenderDecision:
        return RenderDecision(
            source_id=source,
            source_start_us=start * 1_000_000,
            source_end_us=end * 1_000_000,
            action="DROP" if drop else "SPEED",
            output_frame_count=0 if drop else 1,
            provenance="user:process_omission" if drop else "user:process_story",
        )

    original = [decision("a", 0, 600), decision("a", 600, 9600, True), decision("b", 0, 600)]
    split, chapters = work_chapters(original)
    segments = [
        Segment(source_id=s, start_us=0, end_us=600_000_000, kind="SCULPT", score=1)
        for s in ("a", "b")
    ]
    selected = readable_episodes(split, chapters, segments)
    kept = [d for d in selected if d.action != "DROP"]
    assert len(kept) == STORY.chapters
    assert {d.source_id for d in kept} == {"a", "b"}
    assert all(d.source_end_us <= 600_000_000 for d in kept)
    assert all(d.source_end_us - d.source_start_us <= STORY.episode_us for d in kept)
    for source, end in (("a", 9_600_000_000), ("b", 600_000_000)):
        rows = [d for d in selected if d.source_id == source]
        assert rows[0].source_start_us == 0
        assert rows[-1].source_end_us == end
        assert all(
            a.source_end_us == b.source_start_us for a, b in zip(rows, rows[1:], strict=False)
        )


def test_no_work_keeps_exclusions() -> None:
    dropped = RenderDecision(
        source_id="a",
        source_start_us=0,
        source_end_us=100,
        action="DROP",
        output_frame_count=0,
        provenance="user:process_omission",
    )
    split, chapters = work_chapters([dropped])
    assert readable_episodes(split, chapters, []) == [dropped]


def test_continuous_brush_run_wins_over_fragmented_work() -> None:
    choices = [
        RenderDecision(
            source_id="a",
            source_start_us=i * 20_000_000,
            source_end_us=i * 20_000_000 + 15_000_000,
            action="SPEED",
            output_frame_count=1,
            provenance="user:process_story",
        )
        for i in range(2)
    ]
    segments = [
        Segment(source_id="a", start_us=a * 1_000_000, end_us=b * 1_000_000, kind=kind, score=0.9)
        for a, b, kind in (
            (0, 5, "SCULPT"),
            (5, 10, "CAMERA"),
            (10, 15, "SCULPT"),
            (20, 30, "SCULPT"),
            (30, 35, "UNKNOWN"),
        )
    ]
    result = readable_episodes(choices, [0, 0], segments)
    kept = [d for d in result if d.action != "DROP"]
    assert len(kept) == 1
    assert kept[0].source_start_us == 20_000_000


def test_visual_rejection_selects_alternative_without_microcuts() -> None:
    decision = RenderDecision(
        source_id="a",
        source_start_us=0,
        source_end_us=60_000_000,
        action="SPEED",
        output_frame_count=1,
        provenance="user:process_story",
    )
    segments = [Segment(source_id="a", start_us=0, end_us=60_000_000, kind="SCULPT", score=1)]
    selected = readable_episodes(
        [decision],
        [0],
        segments,
        lambda source, start, end: 1.0 if start >= 30_000_000 else 0.0,
    )
    kept = [d for d in selected if d.action != "DROP"]
    assert len(kept) == 1
    assert kept[0].source_start_us >= 30_000_000
    assert kept[0].source_end_us - kept[0].source_start_us == STORY.episode_us
    assert all(
        d.action == "DROP"
        for d in readable_episodes(
            [decision],
            [0],
            segments,
            lambda *args: 0.0,
        )
    )


def test_visual_return_prefers_another_work_moment(monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    monkeypatch.setattr(
        "sculptcut.process_story.STORY",
        replace(STORY, chapters=3, episode_us=2_000_000, candidate_step_us=1_000_000),
    )
    decisions = [
        RenderDecision(
            source_id="a",
            source_start_us=i * 10_000_000,
            source_end_us=i * 10_000_000 + 4_000_000,
            action="SPEED",
            output_frame_count=1,
            provenance="user:process_story",
        )
        for i in range(3)
    ]
    segments = [
        Segment(
            source_id="a",
            start_us=d.source_start_us,
            end_us=d.source_end_us,
            kind="SCULPT",
            score=1,
        )
        for d in decisions
    ]

    def view(_source: str, time_us: int) -> np.ndarray:
        value = 20 if time_us < 10_000_000 or 20_000_000 < time_us < 22_800_000 else 90
        return np.full((64, 64), value, dtype=np.uint8)

    chosen = readable_episodes(decisions, [0, 1, 2], segments, lambda *args: 1.0, view)
    kept = [d for d in chosen if d.action != "DROP"]
    assert len(kept) == 3
    assert kept[-1].source_start_us == 21_000_000
