"""A real local music track must change still cuts and yield decodable video."""

import json
import math
import wave
from pathlib import Path

import cv2
import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from sculptcut.analysis import extract_features
from sculptcut.beat_shorts import (
    BeatStillCandidatesInsufficient,
    _complete_subject,
    _distinct_indices,
    beat_boundaries,
)
from sculptcut.domain import ProjectSettings, Roi
from sculptcut.media import discover, run_process
from sculptcut.planning import plan_project
from sculptcut.project import create_project, save_project
from sculptcut.rendering import render
from sculptcut.ui import MainWindow


def pulse_track(
    path: Path, beats: list[float], seconds: int = 6, strengths: list[float] | None = None
) -> None:
    rate = 8000
    samples = np.zeros(rate * seconds, dtype=np.float64)
    for index, beat in enumerate(beats):
        start = round(beat * rate)
        count = round(0.09 * rate)
        time = np.arange(count) / rate
        volume = strengths[index] if strengths is not None else 0.8
        samples[start : start + count] = volume * np.sin(2 * math.pi * 600 * time)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes((samples * 32767).astype("<i2").tobytes())


def test_beat_still_rejects_subject_cut_by_canvas_edge() -> None:
    viewport = Roi(x=0.1, y=0.1, width=0.8, height=0.8)
    complete = np.full((500, 800), 70, dtype=np.uint8)
    cv2.ellipse(complete, (310, 245), (125, 160), 0, 0, 360, 150, -1)
    cut_off = np.full_like(complete, 70)
    cv2.ellipse(cut_off, (105, 245), (125, 160), 0, 0, 360, 150, -1)
    assert _complete_subject(cv2.cvtColor(complete, cv2.COLOR_GRAY2BGR), viewport)
    assert not _complete_subject(cv2.cvtColor(cut_off, cv2.COLOR_GRAY2BGR), viewport)
    cut_bottom = complete.copy()
    cv2.rectangle(cut_bottom, (180, 260), (480, 449), 150, -1)
    assert not _complete_subject(cv2.cvtColor(cut_bottom, cv2.COLOR_GRAY2BGR), viewport)
    with_palette = cv2.cvtColor(complete, cv2.COLOR_GRAY2BGR)
    cv2.rectangle(with_palette, (200, 200), (310, 290), (0, 0, 255), -1)
    assert not _complete_subject(with_palette, viewport)


def test_beat_still_collapses_identical_views_before_beat_assignment() -> None:
    images = [np.full((80, 48), value, dtype=np.uint8) for value in (20, 20, 20, 35, 50)]
    assert _distinct_indices(images, [0.2, 0.5, 0.3, 0.4, 0.6]) == [1, 3, 4]
    gradual = [np.full((80, 48), value, dtype=np.uint8) for value in (20, 21, 22)]
    assert _distinct_indices(gradual, [0.2, 0.5, 0.3]) == [1]


def test_beat_shorts_plan_and_real_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=160x90:r=10:d=6",
            "-c:v",
            "libx264",
            str(source),
        ]
    )
    music = tmp_path / "beats.wav"
    pulse_track(music, [0.45, 0.9, 1.35, 2.25, 2.7, 3.6, 4.05, 4.5])
    root = tmp_path / "project"
    project = create_project(root, [source]).model_copy(
        update={
            "settings": ProjectSettings(
                preset="beat_shorts",
                target_seconds=3,
                music_path=music,
                music_fade_seconds=0,
                proxy_width=160,
                proxy_fps=5,
            )
        }
    )
    save_project(root, project)
    extract_features(root, project)
    boundaries = beat_boundaries(root, project, 90, None)
    assert boundaries[0] == 0 and boundaries[-1] == 90
    assert len(boundaries) >= 4
    assert len({b - a for a, b in zip(boundaries, boundaries[1:], strict=False)}) > 1
    intensity_music = tmp_path / "intensity.wav"
    pulse_track(
        intensity_music,
        [0.4 * n for n in range(1, 13)],
        strengths=[0.2] * 5 + [0.8] * 7,
    )
    intense = project.model_copy(
        update={"settings": project.settings.model_copy(update={"music_path": intensity_music})}
    )
    intensity_cuts = beat_boundaries(root, intense, 150, None)[1:-1]
    assert sum(60 <= frame < 120 for frame in intensity_cuts) > sum(
        0 < frame < 60 for frame in intensity_cuts
    )
    plan = plan_project(root, project, 3)
    stills = [decision for decision in plan.decisions if decision.action == "HOLD"]
    assert len(stills) >= 2
    assert plan.actual_frame_count == 90
    assert all(shot.subject_camera is not None for shot in stills)
    assert all(shot.provenance == "auto:beat_still" for shot in stills)
    before = source.read_bytes()
    preview = render(root, project, plan, tmp_path / "beat-preview.mp4", preview=True)
    assert preview.is_file()
    output = render(root, project, plan, tmp_path / "beat-shorts.mp4", encoder="libx264")
    assert output.is_file()
    assert source.read_bytes() == before
    hold_index = next(
        index
        for index, decision in enumerate(plan.decisions)
        if decision.action == "HOLD" and decision.output_frame_count >= round(0.7 * plan.output_fps)
    )
    hold = plan.decisions[hold_index]
    first_frame = sum(decision.output_frame_count for decision in plan.decisions[:hold_index])
    capture = cv2.VideoCapture(str(output))
    try:
        capture.set(cv2.CAP_PROP_POS_FRAMES, first_frame)
        ok_start, start_image = capture.read()
        capture.set(cv2.CAP_PROP_POS_FRAMES, first_frame + hold.output_frame_count - 1)
        ok_end, end_image = capture.read()
    finally:
        capture.release()
    assert ok_start and ok_end
    center = (slice(480, 1440), slice(270, 810))
    assert float(cv2.absdiff(start_image[center], end_image[center]).mean()) > 0.01
    run_process([discover("ffmpeg"), "-v", "error", "-i", str(output), "-f", "null", "-"])
    details = json.loads(
        run_process(
            [
                discover("ffprobe"),
                "-v",
                "error",
                "-count_frames",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,nb_read_frames",
                "-of",
                "json",
                str(output),
            ]
        )
    )["streams"][0]
    assert (details["width"], details["height"]) == (1080, 1920)
    assert int(details["nb_read_frames"]) == 90

    changed_music = tmp_path / "different-beats.wav"
    pulse_track(changed_music, [0.6, 1.2, 1.8, 2.4, 3.0, 3.6, 4.2])
    changed = project.model_copy(
        update={"settings": project.settings.model_copy(update={"music_path": changed_music})}
    )
    save_project(root, changed)
    changed_plan = plan_project(root, changed, 3)
    assert [d.output_frame_count for d in changed_plan.decisions if d.action == "HOLD"] != [
        d.output_frame_count for d in stills
    ]

    search_attempts: list[tuple[int, int]] = []

    def no_distinct_stills(*args: object, **kwargs: object) -> None:
        search_attempts.append(
            (int(kwargs["candidates_per_slot"]), int(kwargs["candidate_spacing_us"]))
        )
        raise BeatStillCandidatesInsufficient("synthetic shortage")

    monkeypatch.setattr("sculptcut.beat_shorts._beat_still_plan", no_distinct_stills)
    fallback_plan = plan_project(root, project, 3)
    assert search_attempts == [(24, 5_000_000), (72, 1_000_000)]
    assert fallback_plan.settings["beat_still_fallback"] == "motion"
    assert fallback_plan.settings["story"] == "process"
    assert all(decision.action != "HOLD" or decision.provenance in (
        "user:shorts_final", "user:final_tail",
    ) for decision in fallback_plan.decisions)
    fallback_output = render(
        root, project, fallback_plan, tmp_path / "beat-motion-fallback.mp4", encoder="libx264"
    )
    streams = json.loads(
        run_process(
            [
                discover("ffprobe"),
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "json",
                str(fallback_output),
            ]
        )
    )["streams"]
    assert {stream["codec_type"] for stream in streams} == {"video", "audio"}
    run_process([discover("ffmpeg"), "-v", "error", "-i", str(fallback_output), "-f", "null", "-"])


def test_beat_shorts_is_a_seconds_based_gui_preset(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.show()
        app.processEvents()
        window.preset.setCurrentIndex(window.preset.findData("beat_shorts"))
        assert window.duration_in_seconds
        assert window.target.value() == 60
        assert window.output_suffix.text() == "_beat_shorts.mp4"
        assert "Сменить музыку и пересобрать" in window.change_music_button.text()
        folder = tmp_path / "music"
        folder.mkdir()
        window.music.setText(str(folder))
        calls: list[str] = []
        window.start_action = calls.append  # type: ignore[method-assign]
        window.another_music()
        # No saved video exists here: changing music must not start a new
        # project render. Rebuilding a selected musical version is covered
        # by the real-media Photoshop variant regression.
        assert calls == []
        assert window.job is None
    finally:
        window.close()
