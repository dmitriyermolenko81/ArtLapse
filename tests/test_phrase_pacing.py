"""Readable chronological phrases and a shared continuous playback clock."""

from pathlib import Path
from threading import Event

import cv2
import numpy as np
import pytest
from test_beat_shorts import pulse_track
from test_intro import make_image
from test_timeline import make_source

from sculptcut.analysis import extract_features
from sculptcut.combined_edits import prepare_combined_edit, render_combined_edit
from sculptcut.domain import Override, Project, ProjectSettings, SpeedRamp
from sculptcut.episode_editor import edit_episode
from sculptcut.media import discover, run_process
from sculptcut.overrides import exclude_dropped
from sculptcut.planning import build_plan, plan_project, validate_plan
from sculptcut.project import create_project, save_project
from sculptcut.proxy import make_proxies
from sculptcut.rendering import chunk_decisions, render, verify_output
from sculptcut.segmentation import Segment
from sculptcut.timelapse_phrases import phrase_plan, smooth_phrases
from sculptcut.timewarp import output_fraction, source_time


def test_phrases_keep_late_work_manual_gaps_and_slow_detail() -> None:
    project = Project(
        sources=[make_source(0, 0, 600_000_000)],
        settings=ProjectSettings(target_seconds=60),
        overrides=[
            Override(source_id="0", start_us=570_000_000, end_us=580_000_000, action="DROP")
        ],
    )
    base = exclude_dropped(project, build_plan(project, [], 60))
    segments = [
        Segment(
            source_id="0",
            start_us=i * 20_000_000,
            end_us=(i + 1) * 20_000_000,
            kind="SCULPT" if i % 2 else "UNKNOWN",
            score=0.8,
            importance=0.7,
        )
        for i in range(30)
    ]
    plan = smooth_phrases(phrase_plan(project, base, segments))
    validate_plan(project, plan)
    assert plan.actual_frame_count == plan.target_frame_count == 1800
    active = [d for d in plan.decisions if d.output_frame_count]
    assert any(d.source_start_us >= 580_000_000 for d in active)
    assert all(
        not (d.source_start_us < 580_000_000 and d.source_end_us > 570_000_000) for d in active
    )
    for role in ("context", "detail", "result"):
        assert any(d.provenance == f"story:phrase_{role}" for d in active)
    details = [d for d in active if d.provenance == "story:phrase_detail"]
    fast = [d for d in active if d.provenance == "story:process_bridge"]
    assert fast and any(d.speed_ramp for d in details)

    def speed(d):
        return (d.source_end_us - d.source_start_us) * 30 / (1e6 * d.output_frame_count)

    assert np.median([speed(d) for d in fast]) > np.median([speed(d) for d in details])
    assert type(plan).model_validate_json(plan.model_dump_json()) == plan


def test_continuous_clock_roundtrips_and_is_monotonic() -> None:
    project = Project(sources=[make_source(0, 0, 6_000_000)])
    d = (
        build_plan(project, [], 2)
        .decisions[0]
        .model_copy(
            update={
                "speed_ramp": SpeedRamp(start_us=0, end_us=6_000_000, strength=0.6),
            }
        )
    )
    times = [source_time(d, i / 60) for i in range(61)]
    assert times == sorted(set(times))
    assert times[0] == 0 and times[-1] == 6_000_000
    for i, time in enumerate(times):
        assert output_fraction(d, time) == pytest.approx(i / 60, abs=1e-6)
    assert times[15] > 1_500_000
    assert times[45] < 4_500_000


def test_closing_work_gets_a_phrase_when_final_chapter_prefers_an_earlier_action() -> None:
    project = Project(
        sources=[make_source(0, 0, 3600_000_000)], settings=ProjectSettings(target_seconds=24)
    )
    base = build_plan(project, [], 24)
    segments = [
        Segment(
            source_id="0", start_us=0, end_us=3540_000_000, kind="SCULPT", score=0.9, importance=1
        ),
        Segment(
            source_id="0",
            start_us=3550_000_000,
            end_us=3580_000_000,
            kind="SCULPT",
            score=0.6,
            importance=0.5,
        ),
    ]
    plan = phrase_plan(project, base, segments)
    assert plan.settings["phrase_count"] >= 3
    assert any(
        d.output_frame_count
        and d.provenance == "story:phrase_result"
        and d.source_start_us >= 3500_000_000
        for d in plan.decisions
    )


def test_phrases_keep_recorded_camera_transitions_but_still_remove_confirmed_pauses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sculptcut.idle import IdleRange
    from sculptcut.timelapse_cleanup import clean_timelapse

    project = Project(sources=[make_source(0, 0, 4_000_000)])
    base = build_plan(project, [], 2)
    base = base.model_copy(update={"settings": base.settings | {"story": "phrased_timelapse"}})
    ranges = [
        IdleRange(source_id="0", start_us=0, end_us=1_000_000, reason="timelapse_camera_only"),
        IdleRange(
            source_id="0", start_us=1_000_000, end_us=2_000_000, reason="timelapse_static_view"
        ),
    ]
    monkeypatch.setattr("sculptcut.timelapse_cleanup.cleanup_ranges", lambda *args: ranges)
    plan = clean_timelapse(Path("unused"), project, base)
    assert any(d.output_frame_count and d.source_start_us == 0 for d in plan.decisions)
    assert all(
        not d.output_frame_count or d.source_end_us <= 1_000_000 or d.source_start_us >= 2_000_000
        for d in plan.decisions
    )


def test_long_ramp_keeps_decoder_batches_bounded_and_source_coverage_exact() -> None:
    project = Project(sources=[make_source(0, 0, 900_000_000)])
    plan = build_plan(project, [], 90)
    decision = plan.decisions[0].model_copy(
        update={"speed_ramp": SpeedRamp(start_us=0, end_us=900_000_000, strength=0.65)}
    )
    pieces = chunk_decisions(plan.model_copy(update={"decisions": [decision]}))
    assert pieces[0].source_start_us == 0 and pieces[-1].source_end_us == 900_000_000
    assert all(
        a.source_end_us == b.source_start_us for a, b in zip(pieces, pieces[1:], strict=False)
    )
    assert sum(d.output_frame_count for d in pieces) == 2700
    assert all(d.source_end_us - d.source_start_us <= 300_000_000 for d in pieces)


def test_smooth_speed_respects_a_lower_configured_ceiling() -> None:
    project = Project(sources=[make_source(0, 0, 12_000_000)])
    plan = build_plan(project, [], 2)
    plan = plan.model_copy(
        update={
            "settings": plan.settings | {"story": "phrased_timelapse", "phrase_max_speed": 8},
            "decisions": [
                plan.decisions[0].model_copy(update={"provenance": "story:phrase_detail"})
            ],
        }
    )
    result = smooth_phrases(plan)
    d = result.decisions[0]
    times = [source_time(d, i / 120) for i in range(121)]
    speeds = np.diff(times) / 1e6 * 60
    assert speeds.max() <= 8.01 and speeds.min() >= 1


def test_real_ramp_export_preview_and_trim_use_the_same_clock(tmp_path: Path) -> None:
    source = tmp_path / "moving.mp4"
    writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*"mp4v"), 30, (640, 360))
    assert writer.isOpened()
    for i in range(180):
        frame = np.full((360, 640, 3), 70, np.uint8)
        cv2.rectangle(frame, (55 + i * 2, 140), (65 + i * 2, 220), (0, 0, 240), -1)
        writer.write(frame)
    writer.release()
    music = tmp_path / "music.wav"
    pulse_track(music, [0.5, 1, 1.5], seconds=2)
    root = tmp_path / "project"
    project = create_project(root, [source])
    project = project.model_copy(
        update={
            "settings": ProjectSettings(
                preset="timelapse",
                target_seconds=2,
                proxy_fps=30,
                proxy_width=640,
                final_view_seconds=0,
                music_path=music,
            )
        }
    )
    save_project(root, project)
    make_proxies(root, project)
    base = build_plan(project, [], 2)
    d = base.decisions[0].model_copy(
        update={"speed_ramp": SpeedRamp(start_us=0, end_us=6_000_000, strength=0.6)}
    )
    plan = base.model_copy(update={"decisions": [d]})
    validate_plan(project, plan)
    positions = []
    for preview in (False, True):
        output = root / "renders" / f"ramp-{preview}.mp4"
        render(root, project, plan, output, Event(), preview=preview, encoder="libx264")
        verify_output(output, 60, 30, expected_audio=True)
        reader = cv2.VideoCapture(str(output))
        xs = []
        try:
            while True:
                ok, frame = reader.read()
                if not ok:
                    break
                ys, x = np.where((frame[:, :, 2] > 150) & (frame[:, :, 2] > frame[:, :, 1] * 1.5))
                assert len(x) > 50
                xs.append(float(np.mean(x)))
        finally:
            reader.release()
        assert len(xs) == 60
        delta = np.diff(xs)
        assert np.median(delta[:8]) > np.median(delta[26:34]) * 1.8
        assert np.median(delta[-8:]) > np.median(delta[26:34]) * 1.8
        positions.append(xs)
    assert np.max(np.abs(np.asarray(positions[0]) - positions[1])) <= 5
    edit = prepare_combined_edit(root, root / "renders/ramp-False.mp4", [(500_000, 1_000_000)])
    removed = [d for d in edit.plan.decisions if d.provenance == "user:shorts_manual_cut"]
    assert len(removed) == 1
    assert removed[0].source_start_us == source_time(d, 0.25)
    assert removed[0].source_end_us == source_time(d, 0.5)
    trimmed = root / "renders/trimmed.mp4"
    render_combined_edit(root, edit, trimmed, Event(), lambda _: None, encoder="libx264")
    verify_output(trimmed, 45, 30, expected_audio=True)
    reader = cv2.VideoCapture(str(trimmed))
    retained = []
    try:
        while True:
            ok, frame = reader.read()
            if not ok:
                break
            _, x = np.where((frame[:, :, 2] > 150) & (frame[:, :, 2] > frame[:, :, 1] * 1.5))
            assert len(x) > 50
            retained.append(float(np.mean(x)))
    finally:
        reader.release()
    assert (
        np.max(np.abs(np.asarray(retained) - np.asarray(positions[0][:15] + positions[0][30:])))
        <= 5
    )


def test_real_phrase_pipeline_edits_and_final_fade_keep_the_total_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "process.mp4"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 10, (640, 360))
    assert writer.isOpened()
    for i in range(900):
        frame = np.full((360, 640, 3), 20, np.uint8)
        frame[40:320, 80:560] = 70
        radius = 65 if i < 300 or i >= 600 else 105
        cv2.ellipse(frame, (320, 190), (radius, 100), 0, 0, 360, (150, 150, 150), -1)
        for stroke in range((i % 100) // 10 + 1):
            cv2.line(frame, (290, 150 + stroke * 5), (335, 150 + stroke * 5),
                     (50, 50, 50), 2)
        writer.write(frame)
    writer.release()
    music = tmp_path / "music.wav"
    pulse_track(music, [i + 0.2 for i in range(20)], seconds=20)
    image = tmp_path / "cover.png"
    make_image(image, (0, 50, 70))
    root = tmp_path / "project"
    project = create_project(root, [video])
    project = project.model_copy(
        update={
            "settings": ProjectSettings(
                target_seconds=20,
                proxy_width=640,
                proxy_fps=5,
                music_path=music,
                intro_image=image,
                final_view_seconds=1,
            )
        }
    )
    save_project(root, project)
    extract_features(root, project)
    plan = plan_project(root, project, 20)
    assert plan.settings["story"] == "phrased_timelapse"
    assert int(plan.settings["work_candidate_count"]) > int(plan.settings["work_selected_count"])
    assert plan.actual_frame_count == plan.target_frame_count == 555
    assert any(d.speed_ramp is not None for d in plan.decisions)
    monkeypatch.setattr("sculptcut.rendering.export_size", lambda *args: (320, 180))
    first = root / "renders/phrases.mp4"
    render(root, project, plan, first, encoder="libx264")
    verify_output(first, 600, 30, expected_audio=True)
    removed = next(d for d in plan.decisions if d.speed_ramp is not None)
    changed = edit_episode(project, removed, None)
    save_project(root, changed)
    edit = prepare_combined_edit(root, first)
    assert edit.plan.actual_frame_count == 555
    assert not edit.plan.settings.get("work_needs_quality_check")
    scene_frames: dict[str, int] = {}
    for decision in edit.plan.decisions:
        if decision.episode_id and decision.provenance == "story:work_highlight":
            scene_frames[decision.episode_id] = (
                scene_frames.get(decision.episode_id, 0) + decision.output_frame_count
            )
    assert scene_frames and max(scene_frames.values()) <= 90
    assert all(
        not d.output_frame_count
        or d.source_end_us <= removed.source_start_us
        or d.source_start_us >= removed.source_end_us
        for d in edit.plan.decisions
    )
    output = root / "renders/edited.mp4"
    render_combined_edit(root, edit, output, Event(), lambda _: None, encoder="libx264")
    verify_output(output, 600, 30, expected_audio=True)
    for index, path in enumerate((first, output)):
        pcm = tmp_path / f"fade-{index}.f32"
        run_process(
            [
                discover("ffmpeg"),
                "-v",
                "error",
                "-i",
                str(path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "48000",
                "-f",
                "f32le",
                str(pcm),
            ]
        )
        audio = np.fromfile(pcm, dtype="<f4")

        def rms(t, samples=audio):
            return float(
                np.sqrt(np.mean(samples[round(t * 48000) : round((t + 0.03) * 48000)] ** 2))
            )

        levels = [rms(t) for t in (15.22, 16.22, 17.22, 18.22, 19.22)]
        assert all(a > b for a, b in zip(levels, levels[1:], strict=False))
        assert levels[0] > 0.9 * rms(14.22) and levels[-1] < levels[0] * 0.1
