"""Visual persistence, conservative fallback and chronological editorial effects."""

from dataclasses import replace
from pathlib import Path
from threading import Event

import cv2
import numpy as np
import pytest
from test_timeline import make_source

from sculptcut.development import (
    SETTINGS,
    DevelopmentEvidence,
    chronological_weights,
    development_weight,
    measure_development,
)
from sculptcut.domain import Project, ProjectSettings, Roi
from sculptcut.media import Cancelled, discover, run_process
from sculptcut.planning import RenderDecision, build_plan, validate_plan
from sculptcut.process_story import STORY, readable_episodes
from sculptcut.project import create_project
from sculptcut.proxy import make_proxies
from sculptcut.rendering import render, verify_output
from sculptcut.segmentation import Segment
from sculptcut.shorts_quality import EpisodeInspector
from sculptcut.timelapse_story import episodic_timelapse_plan


def textured_frame() -> np.ndarray:
    rng = np.random.default_rng(73)
    return cv2.GaussianBlur(rng.integers(35, 220, (160, 240), dtype=np.uint8), (3, 3), 0)


def test_persistent_work_survives_while_cursor_and_navigation_do_not_get_a_bonus() -> None:
    before = textured_frame()
    after = before.copy()
    after[55:85, 85:115] = 225
    work = measure_development([before] * 3 + [after] * 6)
    assert work.score is not None and work.score > 0.5
    assert development_weight(work) > 1
    # One-frame cursor glitches disappear in temporal medians; temporary edits
    # returning to the original image do not become persistent development.
    for frames in (
        [before] * 9,
        [before, after, before] * 3,
        [before] * 3 + [after] * 3 + [before] * 3,
    ):
        evidence = measure_development(frames)
        assert evidence.score == 0
        assert development_weight(evidence) < 1
    cursor_before, cursor_after = before.copy(), before.copy()
    cv2.circle(cursor_before, (45, 60), 14, 240, 1)
    cv2.circle(cursor_after, (110, 60), 14, 240, 1)
    cursor = measure_development([cursor_before] * 3 + [cursor_after] * 6)
    assert development_weight(cursor) <= 1
    transforms = [
        np.float32([[1, 0, 8], [0, 1, 0]]),
        cv2.getRotationMatrix2D((120, 80), 0, 1.12),
        cv2.getRotationMatrix2D((120, 80), 12, 1),
    ]
    moved = [cv2.warpAffine(before, matrix, (240, 160)) for matrix in transforms]
    moved.append(np.clip(before.astype(np.int16) + 40, 0, 255).astype(np.uint8))
    for image in moved:
        evidence = measure_development([before] * 3 + [image] * 6)
        assert evidence.score is None
        assert development_weight(evidence) == 1
    assert measure_development([np.zeros_like(before)] * 9).score is None


def test_repeated_states_only_affect_comparable_windows_from_the_same_source() -> None:
    before = textured_frame()
    after = before.copy()
    after[55:85, 85:115] = 225
    work = measure_development([before] * 3 + [after] * 6)
    windows = [("a", 0, 6), ("a", 6, 12), ("b", 0, 6), ("a", 12, 18), ("a", 18, 24)]
    scores = chronological_weights(
        windows, lambda source, start, end: DevelopmentEvidence() if start == 12 else work,
    )
    assert scores[windows[1]] < scores[windows[0]]
    assert scores[windows[2]] == scores[windows[0]]
    assert scores[windows[3]] == 1
    assert scores[windows[4]] == scores[windows[0]]


def test_development_changes_selection_and_budget_without_losing_temporal_chapters() -> None:
    source = make_source(0, 0, 120_000_000)
    project = Project(sources=[source], settings=ProjectSettings(preset="timelapse"))
    segments = [Segment(source_id=source.id, start_us=0, end_us=source.duration_us,
                        kind="SCULPT", score=0.9)]
    base = build_plan(project, [], 16)
    unchanged = episodic_timelapse_plan(project, base, segments)
    def unknown(_source: str, start: int, end: int) -> DevelopmentEvidence:
        return DevelopmentEvidence()

    assert episodic_timelapse_plan(project, base, segments, unknown) == unchanged

    def development(_source: str, start: int, end: int) -> DevelopmentEvidence:
        return DevelopmentEvidence(1.0 if 20_000_000 <= start < 60_000_000 else 0.0)

    plan = episodic_timelapse_plan(project, base, segments, development)
    validate_plan(project, plan)
    kept = [d for d in plan.decisions if d.action != "DROP"]
    assert len(kept) == 2
    assert 20_000_000 <= kept[0].source_start_us < 60_000_000 <= kept[1].source_start_us
    assert kept[0].output_frame_count > kept[1].output_frame_count
    assert plan.actual_frame_count == 480
    assert all((d.source_end_us - d.source_start_us) * 30
               <= d.output_frame_count * 8_000_000 for d in kept)
    assert all(a.source_end_us == b.source_start_us
               for a, b in zip(plan.decisions, plan.decisions[1:], strict=False))

    choices = [RenderDecision(
        source_id=source.id, source_start_us=0, source_end_us=40_000_000,
        action="SPEED", output_frame_count=1, provenance="user:process_story",
    )]
    selected = readable_episodes(
        choices, [0], segments, lambda *_: 1.0, development=development,
    )
    short = [d for d in selected if d.action != "DROP"]
    assert len(short) == 1 and short[0].source_start_us >= 20_000_000
    assert short[0].source_end_us - short[0].source_start_us == STORY.episode_us


def test_real_video_inspection_cache_cancellation_and_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gray = np.full((160, 240), 45, dtype=np.uint8)
    gray[20:140, 30:210] = (textured_frame()[20:140, 30:210] * 0.4 + 90).astype(np.uint8)
    video = tmp_path / "development.mp4"
    raw = tmp_path / "source.rgb"
    with raw.open("wb") as stream:
        for i in range(150):
            image = gray.copy()
            if i >= 30:
                side = min(30, 2 + (i - 30) * 2)
                image[55:55 + side, 85:85 + side] = 225
            stream.write(cv2.cvtColor(image, cv2.COLOR_GRAY2RGB).tobytes())
    run_process([
        discover("ffmpeg"), "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", "240x160", "-r", "5", "-i", str(raw), "-c:v", "libx264",
        "-threads", "1", "-pix_fmt", "yuv420p", str(video),
    ])
    root = tmp_path / "project"
    project = create_project(root, [video])
    source = project.sources[0]
    project = project.model_copy(update={
        "settings": ProjectSettings(proxy_width=240, proxy_fps=5, output_fps=10),
        "rois": {source.id: Roi(x=0, y=0, width=1, height=1)},
    })
    make_proxies(root, project)
    cancel = Event()
    with EpisodeInspector(root, project, cancel) as inspector:
        work = inspector.development(source.id, 5_000_000, 11_000_000)
        static = inspector.development(source.id, 0, 5_000_000)
        assert work.score is not None and work.score > 0.3
        assert static.score == 0
        reader = inspector.readers[source.id]
        position = reader.get(cv2.CAP_PROP_POS_FRAMES)
        assert inspector.development(source.id, 5_000_000, 11_000_000) is work
        assert reader.get(cv2.CAP_PROP_POS_FRAMES) == position
        cancel.set()
        with pytest.raises(Cancelled):
            inspector.development(source.id, 5_000_000, 11_000_000)
        cancel.clear()
        monkeypatch.setattr(
            "sculptcut.shorts_quality.DEVELOPMENT", replace(SETTINGS, cache_entries=2),
        )
        inspector.development(source.id, 20_000_000, 25_000_000)
        assert len(inspector.inspections) <= 2
        segments = [Segment(source_id=source.id, start_us=0, end_us=source.duration_us,
                            kind="SCULPT", score=0.9)]
        plan = episodic_timelapse_plan(
            project, build_plan(project, [], 4), segments, inspector.development,
        )
    validate_plan(project, plan)
    output = render(root, project, plan, tmp_path / "result.mp4", encoder="libx264")
    verify_output(output, plan.actual_frame_count, plan.output_fps)
    run_process([discover("ffmpeg"), "-v", "error", "-i", str(output), "-f", "null", "-"])


def test_shorts_give_more_time_to_development_and_keep_every_chapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sculptcut.process_story import process_plan

    monkeypatch.setattr("sculptcut.process_story.STORY", replace(STORY, chapters=3))
    monkeypatch.setattr("sculptcut.process_story.project_screens", lambda *_: [])
    monkeypatch.setattr("sculptcut.sculpt_only.final_start", lambda *_: None)
    monkeypatch.setattr("sculptcut.shorts_quality.EpisodeInspector.score", lambda *_: 1.0)
    monkeypatch.setattr("sculptcut.shorts_quality.EpisodeInspector.signature", lambda *_: None)

    def development(
        self: EpisodeInspector, source_id: str, start: int, end: int,
    ) -> DevelopmentEvidence:
        return DevelopmentEvidence(1.0 if start < 30_000_000 else 0.0)

    monkeypatch.setattr("sculptcut.shorts_quality.EpisodeInspector.development", development)
    source = make_source(0, 0, 90_000_000)
    project = Project(sources=[source], settings=ProjectSettings(preset="shorts"))
    segments = [Segment(source_id=source.id, start_us=0, end_us=source.duration_us,
                        kind="SCULPT", score=0.9)]
    plan = process_plan(tmp_path, project, build_plan(project, [], 12), segments)
    validate_plan(project, plan)
    kept = [d for d in plan.decisions if d.action != "DROP"]
    assert len(kept) == 3
    assert [d.source_start_us // 30_000_000 for d in kept] == [0, 1, 2]
    assert kept[0].output_frame_count > kept[1].output_frame_count
    assert plan.actual_frame_count == 360
    assert all((d.source_end_us - d.source_start_us) * 30
               <= d.output_frame_count * 8_000_000 for d in kept)
