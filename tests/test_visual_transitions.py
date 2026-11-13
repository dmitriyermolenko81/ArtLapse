"""Similar cut boundaries prefer vetted alternatives while preserving source-time limits."""

from dataclasses import replace
from pathlib import Path
from threading import Event

import numpy as np
import pytest
from test_timeline import make_source

from sculptcut.domain import Override, Project, ProjectSettings, Roi
from sculptcut.media import Cancelled, discover, run_process
from sculptcut.photoshop_music import photoshop_music_plan
from sculptcut.photoshop_stability import VIEW_SETTINGS, DrawingViews
from sculptcut.planning import RenderDecision, build_plan
from sculptcut.process_story import readable_episodes
from sculptcut.project import create_project, fingerprint
from sculptcut.segmentation import Segment
from sculptcut.visual_transitions import choose_distinct_view, view_difference


def view(value: int) -> np.ndarray:
    return np.full((64, 64), value, np.uint8)


@pytest.mark.parametrize("case", ["replace", "poor", "unknown", "protected", "clear"])
def test_choose_replacement_only_when_similar_and_safe(case: str) -> None:
    images = [view(21), view(60), view(100)]
    scores = [1, .9, .1]
    if case == "poor":
        scores[1] = .2
    if case == "clear":
        images[0] = view(40)
    calls = []

    def signature(index: int):
        calls.append(index)
        return None if case == "unknown" else images[index]

    chosen = choose_distinct_view(scores, signature, view(20), 0,
                                 lambda index: case != "protected")
    assert chosen == (1 if case == "replace" else 0)
    if case in ("unknown", "clear"):
        assert calls == [0]
    assert choose_distinct_view(scores, signature, None, 0) == 0
    assert choose_distinct_view([1, .9], lambda index: view(21 + index), view(20), 0) == 0


@pytest.mark.parametrize("case", ["replace", "poor", "unknown", "protected", "clear"])
def test_moving_episode_compares_previous_end_to_next_start(case: str) -> None:
    decisions = [RenderDecision(source_id="0", source_start_us=a * 1_000_000,
        source_end_us=b * 1_000_000, action="SPEED", output_frame_count=1,
        provenance="user:process_story") for a, b in ((0, 6), (20, 30))]
    segments = [Segment(source_id="0", start_us=d.source_start_us,
                        end_us=d.source_end_us, kind="SCULPT", score=1) for d in decisions]

    def signature(source: str, timestamp: int):
        if timestamp < 10_000_000:
            return view(20)
        if timestamp == 20_000_000:
            return None if case == "unknown" else view(40 if case == "clear" else 21)
        if timestamp == 22_000_000:
            return view(60)
        return view(90)  # The baseline's ending differs, but its opening is nearly identical.

    def quality(source: str, start: int, end: int) -> float:
        if start >= 24_000_000:
            return .1
        return .2 if start == 22_000_000 and case == "poor" else 1

    selected = readable_episodes(decisions, [0, 1], segments, quality, signature,
        chapter_count=2, prefer_distinct=True,
        can_replace=lambda source, start, end: case != "protected")
    kept = [d for d in selected if d.action != "DROP"]
    assert [d.source_start_us for d in kept] == [0, (22 if case == "replace" else 20) * 1_000_000]
    assert all(d.source_end_us - d.source_start_us == 6_000_000 for d in kept)
    assert selected[0].source_start_us == 0 and selected[-1].source_end_us == 30_000_000
    assert all(a.source_end_us == b.source_start_us or a.source_end_us == 6_000_000
               for a, b in zip(selected, selected[1:], strict=False))
    ordinary = readable_episodes(decisions, [0, 1], segments, quality, signature, chapter_count=2)
    assert [d.source_start_us for d in ordinary if d.action != "DROP"] == [0, 20_000_000]


def test_real_drawing_views_ignore_ui_and_bound_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "drawing.mp4"
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
        "color=c=0x222222:s=320x180:r=10:d=3,"
        "drawbox=x=80:y=20:w=160:h=140:c=0x999999:t=fill,"
        "drawbox=x=110:y=40:w=80:h=100:c=0x555555:t=fill,"
        "drawbox=x=0:y=0:w=50:h=180:c=red:t=fill:enable='gte(t,1)',"
        "drawbox=x=130:y=80:w=60:h=50:c=white:t=fill:enable='gte(t,2)'",
        "-c:v", "libx264", str(video)])
    before = fingerprint(video)
    project = create_project(tmp_path / "project", [video])
    source = project.sources[0]
    project = project.model_copy(update={"rois": {
        source.id: Roi(x=.25, y=1/9, width=.5, height=7/9),
    }})
    monkeypatch.setattr("sculptcut.photoshop_stability.VIEW_SETTINGS",
                        replace(VIEW_SETTINGS, cache_entries=2))
    cancel = Event()
    with DrawingViews(project, cancel) as views:
        first, same, different = [views.signature(source, t)
                                  for t in (500_000, 1_500_000, 2_500_000)]
        assert first is not None and same is not None and different is not None
        assert view_difference(first, same) < 2
        assert view_difference(same, different) >= 8
        assert len(views.cache) == 2
        cancel.set()
        with pytest.raises(Cancelled):
            views.signature(source, 2_500_000)
    assert fingerprint(video) == before


def test_unknown_previous_end_does_not_compare_an_older_episode() -> None:
    decisions = [RenderDecision(source_id="0", source_start_us=a * 1_000_000,
        source_end_us=b * 1_000_000, action="SPEED", output_frame_count=1,
        provenance="user:process_story") for a, b in ((0, 6), (20, 26), (40, 50))]
    segments = [Segment(source_id="0", start_us=d.source_start_us,
                        end_us=d.source_end_us, kind="SCULPT", score=1) for d in decisions]

    def signature(source, timestamp):
        if 25_000_000 < timestamp < 26_000_000:
            return None
        return view(20 if timestamp < 10_000_000 else 21 if timestamp == 40_000_000 else 60)

    result = readable_episodes(decisions, [0, 1, 2], segments, lambda *args: 1.0, signature,
                                chapter_count=3, prefer_distinct=True)
    assert [d.source_start_us for d in result if d.action != "DROP"] == [0, 20_000_000, 40_000_000]


@pytest.mark.parametrize("protected", [False, True])
def test_drawing_episode_replacement_preserves_grid_and_manual_windows(protected: bool) -> None:
    source = make_source(0, 0, 120_000_000)
    project = Project(sources=[source], settings=ProjectSettings(preset="photoshop_music_shorts"),
                      overrides=[Override(source_id="0", start_us=40_000_000,
                                          end_us=53_000_000, action="SET_SPEED", speed=8)]
                      if protected else [])
    base = build_plan(project, [], 3)

    def score(source, start, end):
        return .05 if start == 54_000_000 else 1 if 60_000_000 < start < 80_000_000 else 0

    def signature(source, timestamp):
        return view(20 if timestamp < 40_000_000 else 21 if timestamp < 53_000_000 else 70)

    result = photoshop_music_plan(project, base, [0, 30, 60, 90], score,
                                   view_signature=signature)
    active = [d for d in result.decisions if d.output_frame_count]
    assert [d.output_frame_count for d in active] == [30, 30, 30]
    assert active[1].source_start_us == (40_666_666 if protected else 54_000_000)
    assert active[-1].source_end_us == source.duration_us
    assert all(d.source_end_us - d.source_start_us == 12_000_000 for d in active)
