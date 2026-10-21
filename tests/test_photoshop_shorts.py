from pathlib import Path

import cv2
import numpy as np
from PySide6.QtWidgets import QApplication

from sculptcut.domain import ProjectSettings, Roi
from sculptcut.media import discover, probe, run_process
from sculptcut.output_naming import output_suffix
from sculptcut.planning import plan_project, validate_plan
from sculptcut.project import create_project, load_project, save_project
from sculptcut.rendering import render, verify_output
from sculptcut.shorts_refill import refill_plan_after_cuts
from sculptcut.ui import MainWindow


def test_photoshop_idle_keeps_changes_and_cuts_sustained_stillness() -> None:
    from sculptcut.photoshop_idle import detect_photoshop_idle

    def frames() -> list[np.ndarray]:
        images = []
        for index in range(100):
            image = np.full((180, 320, 3), 220, np.uint8)
            if index >= 40:
                cv2.rectangle(image, (80, 40), (160, 140), (30, 30, 30), -1)
            if index >= 90:
                cv2.line(image, (90, 50), (150, 120), (0, 0, 0), 2)
            images.append(image)
        return images

    ranges = detect_photoshop_idle(frames(), "source-0000", 50_000_000)
    assert len(ranges) == 2
    assert ranges[0].start_us > 0 and ranges[0].end_us < 20_000_000
    assert ranges[1].start_us > 20_000_000 and ranges[1].end_us < 45_000_000


def test_photoshop_idle_keeps_accumulating_small_brush_strokes() -> None:
    from sculptcut.photoshop_idle import detect_photoshop_idle

    frames = []
    for index in range(80):
        image = np.full((180, 320, 3), 220, np.uint8)
        for stroke in range(index):
            x = 10 + (stroke % 20) * 10
            y = 10 + (stroke // 20) * 20
            cv2.rectangle(image, (x, y), (x + 5, y + 5), (20, 20, 20), -1)
        frames.append(image)
    assert detect_photoshop_idle(frames, "source-0000", 40_000_000) == []


def test_stability_score_prefers_fixed_canvas_with_drawing_changes() -> None:
    from sculptcut.photoshop_stability import stability_score

    steady = []
    moving = []
    for index in range(12):
        fixed = np.full((180, 320, 3), (210, 180, 150), np.uint8)
        shifted = fixed.copy()
        cv2.rectangle(fixed, (90, 25), (170, 155), (20, 35, 160), -1)
        cv2.rectangle(shifted, (90 + 5 * index, 25), (170 + 5 * index, 155), (20, 35, 160), -1)
        cv2.circle(fixed, (100 + 4 * index, 60), 3, (0, 0, 0), -1)
        steady.append(fixed)
        moving.append(shifted)
    assert stability_score(steady) < stability_score(moving)


def test_stability_analysis_of_synthetic_source_prefers_fixed_canvas(tmp_path: Path) -> None:
    from sculptcut.photoshop_stability import score_source_window

    video = tmp_path / "pan-then-fixed.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=0xaaccdd:s=320x180:r=5:d=30",
            "-f",
            "lavfi",
            "-i",
            "color=c=0xff3300:s=50x100:r=5:d=30",
            "-filter_complex",
            "[0:v][1:v]overlay=x='60+4*min(t,15)':y=40:shortest=1",
            "-c:v",
            "libx264",
            str(video),
        ]
    )
    root = tmp_path / "project"
    source = create_project(root, [video]).sources[0]
    moving = score_source_window(root, source, 0, 15_000_000, None)
    fixed = score_source_window(root, source, 15_000_000, 30_000_000, None)
    assert fixed < moving
    assert score_source_window(root, source, 15_000_000, 30_000_000, None) == fixed


def test_long_photoshop_short_selects_readable_chronological_episodes(tmp_path: Path) -> None:
    from sculptcut.photoshop_story import STORY, episodic_photoshop_plan
    from sculptcut.planning import build_plan

    video = tmp_path / "brief.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x64:r=1:d=1",
            "-c:v",
            "libx264",
            str(video),
        ]
    )
    root = tmp_path / "project"
    original = create_project(root, [video])
    source = original.sources[0].model_copy(update={"duration_us": 7_200_000_000})
    project = original.model_copy(
        update={
            "sources": [source],
            "settings": ProjectSettings(preset="photoshop_shorts", output_fps=30),
        }
    )
    plan = episodic_photoshop_plan(project, build_plan(project, [], 60))
    kept = [d for d in plan.decisions if d.action != "DROP"]
    episodes = [d for d in kept if d.provenance == "photoshop:episode"]
    final = [d for d in kept if d.provenance == "user:final_tail"]
    assert len(episodes) == STORY.episodes
    assert len(final) == 1
    assert plan.actual_frame_count == 1800
    assert plan.settings["mode"] == "photoshop_episodes"
    assert len(episodes) >= 40
    assert max(d.output_frame_count for d in episodes) <= 45
    source_seconds = sum(d.source_end_us - d.source_start_us for d in episodes) / 1_000_000
    output_seconds = sum(d.output_frame_count for d in episodes) / 30
    assert source_seconds / output_seconds >= 9
    assert all(d.source_end_us - d.source_start_us <= STORY.episode_us for d in episodes)
    assert all(
        (d.source_end_us - d.source_start_us) * 30
        <= d.output_frame_count * 1_000_000 * STORY.max_speed
        for d in episodes
    )
    assert final[0].source_start_us == source.duration_us - STORY.final_us
    assert final[0].source_end_us == source.duration_us
    assert final[0].output_frame_count == 4 * plan.output_fps
    assert kept[0].source_start_us < 300_000_000
    assert kept[-1].source_start_us > 6_800_000_000
    assert sum(d.action == "DROP" for d in plan.decisions) >= STORY.episodes
    validate_plan(project, plan)


def test_photoshop_chapter_prefers_stable_candidate(tmp_path: Path) -> None:
    from sculptcut.photoshop_story import episodic_photoshop_plan
    from sculptcut.planning import build_plan

    video = tmp_path / "brief.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x64:r=1:d=1",
            "-c:v",
            "libx264",
            str(video),
        ]
    )
    original = create_project(tmp_path / "project", [video])
    source = original.sources[0].model_copy(update={"duration_us": 7_200_000_000})
    project = original.model_copy(
        update={
            "sources": [source],
            "settings": ProjectSettings(preset="photoshop_shorts", output_fps=30),
        }
    )
    calls: list[int] = []

    def score(_source: object, start: int, _end: int) -> float:
        calls.append(start)
        return float(start)

    plan = episodic_photoshop_plan(project, build_plan(project, [], 60), score)
    episodes = [d for d in plan.decisions if d.provenance == "photoshop:episode"]
    assert len(calls) == 45 * 3
    assert len(episodes) == 45
    assert episodes[0].source_start_us < 60_000_000


def test_short_photoshop_budget_reduces_episode_count_to_preserve_duration(tmp_path: Path) -> None:
    from sculptcut.photoshop_story import STORY, episodic_photoshop_plan
    from sculptcut.planning import build_plan

    video = tmp_path / "brief.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x64:r=1:d=1",
            "-c:v",
            "libx264",
            str(video),
        ]
    )
    root = tmp_path / "project"
    original = create_project(root, [video])
    source = original.sources[0].model_copy(update={"duration_us": 7_200_000_000})
    project = original.model_copy(
        update={
            "sources": [source],
            "settings": ProjectSettings(preset="photoshop_shorts", output_fps=30),
        }
    )
    plan = episodic_photoshop_plan(project, build_plan(project, [], 15))
    episodes = [d for d in plan.decisions if d.provenance == "photoshop:episode"]
    assert 5 < len(episodes) < STORY.episodes
    assert plan.actual_frame_count == 15 * 30
    assert all(d.output_frame_count > 0 for d in episodes)
    validate_plan(project, plan)


def test_faster_photoshop_episode_plan_renders_short_video(tmp_path: Path) -> None:
    from sculptcut.photoshop_story import episodic_photoshop_plan
    from sculptcut.planning import build_plan

    video = tmp_path / "long-source.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=64x64:r=1:d=140",
            "-c:v",
            "libx264",
            str(video),
        ]
    )
    root = tmp_path / "project"
    project = create_project(root, [video]).model_copy(
        update={"settings": ProjectSettings(preset="photoshop_shorts", output_fps=1)}
    )
    plan = episodic_photoshop_plan(project, build_plan(project, [], 15))
    assert sum(d.provenance == "photoshop:episode" for d in plan.decisions) > 5
    assert plan.actual_frame_count == 15
    output = render(root, project, plan, tmp_path / "faster-short.mp4", encoder="libx264")
    verify_output(output, 15, 1)


def test_photoshop_final_survives_idle_tail_and_stays_at_normal_speed(tmp_path: Path) -> None:
    from sculptcut.idle import IdleRange
    from sculptcut.photoshop_story import episodic_photoshop_plan
    from sculptcut.planning import build_plan

    video = tmp_path / "brief.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x64:r=1:d=1",
            "-c:v",
            "libx264",
            str(video),
        ]
    )
    root = tmp_path / "project"
    original = create_project(root, [video])
    source = original.sources[0].model_copy(update={"duration_us": 1_000_000_000})
    project = original.model_copy(
        update={
            "sources": [source],
            "settings": ProjectSettings(preset="photoshop_shorts", output_fps=30),
        }
    )
    base = build_plan(
        project,
        [
            IdleRange(
                source_id=source.id,
                start_us=980_000_000,
                end_us=source.duration_us,
                reason="photoshop:visually_static",
            )
        ],
        60,
    )
    plan = episodic_photoshop_plan(project, base)
    final = [d for d in plan.decisions if d.provenance == "user:final_tail"]
    assert sum(d.source_end_us - d.source_start_us for d in final) == 4_000_000
    assert sum(d.output_frame_count for d in final) == 120
    assert plan.actual_frame_count == 1800
    validate_plan(project, plan)


def test_photoshop_plan_removes_static_source_time_and_renders(tmp_path: Path) -> None:
    from sculptcut.edit_stats import statistics_text

    video = tmp_path / "pauses.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=0xaaccdd:s=320x180:r=2:d=42",
            "-vf",
            "drawbox=x=120:y=50:w=60:h=90:color=red:t=fill:enable='gte(t,20)'",
            "-c:v",
            "libx264",
            str(video),
        ]
    )
    root = tmp_path / "project"
    project = create_project(root, [video]).model_copy(
        update={"settings": ProjectSettings(preset="photoshop_shorts", output_fps=1)}
    )
    plan = plan_project(root, project, 60)
    assert sum(d.action == "DROP" for d in plan.decisions) >= 2
    assert any(d.action == "SPEED" for d in plan.decisions)
    assert "Вырезано: 0:00:00" not in statistics_text(plan)
    validate_plan(project, plan)
    assert plan_project(root, project, 60) == plan
    output = render(root, project, plan, tmp_path / "trimmed.mp4", encoder="libx264")
    verify_output(output, plan.actual_frame_count, 1)


def test_photoshop_short_export_and_refill(tmp_path: Path) -> None:
    from sculptcut.cli import main

    video = tmp_path / "drawing.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=160x120:r=5:d=70",
            "-c:v",
            "libx264",
            str(video),
        ]
    )
    root = tmp_path / "project"
    project = create_project(root, [video]).model_copy(
        update={"settings": ProjectSettings(preset="photoshop_shorts", output_fps=1)}
    )
    save_project(root, project)
    assert load_project(root).settings.preset == "photoshop_shorts"
    plan = plan_project(root, project, 60)
    assert plan.actual_frame_count == plan.target_frame_count == 60
    assert all(
        d.subject_camera is not None and d.subject_camera.fit_subject for d in plan.decisions
    )
    assert all(d.action == "SPEED" for d in plan.decisions)
    assert plan.decisions[-1].source_end_us == project.sources[0].duration_us
    assert plan_project(root, project, 60) == plan
    filled = refill_plan_after_cuts(project, plan, [(2_000_000, 5_000_000)])
    validate_plan(project, filled)
    assert filled.actual_frame_count == 60
    assert any(d.action == "DROP" for d in filled.decisions)
    output = render(root, project, plan, tmp_path / "photoshop.mp4", encoder="libx264")
    assert (probe(output).width, probe(output).height) == (1080, 1920)
    verify_output(output, 60, 1)
    assert output_suffix(project.settings.preset) == "_photoshop_shorts"
    assert main(["plan", str(root), "--preset", "photoshop_shorts"]) == 0


def test_drawing_tracking_keeps_moving_subject_complete(tmp_path: Path) -> None:
    from sculptcut.photoshop import drawing_box
    from sculptcut.subject_camera import camera_preview

    video = tmp_path / "moving-drawing.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=0xaaccdd:s=640x360:r=15:d=2",
            "-f",
            "lavfi",
            "-i",
            "color=c=0xff3300:s=100x200:r=15:d=2",
            "-filter_complex",
            "[0:v][1:v]overlay=x='60+180*t':y=80:shortest=1",
            "-c:v",
            "libx264",
            str(video),
        ]
    )
    root = tmp_path / "drawing-project"
    project = create_project(root, [video]).model_copy(
        update={"settings": ProjectSettings(preset="photoshop_shorts", output_fps=15)}
    )
    plan = plan_project(root, project, 60)
    camera = plan.decisions[0].subject_camera
    assert camera is not None and camera.fit_subject
    assert len(camera.points) == 1
    # A fixed camera must contain the entire source pan rather than chase it.
    assert camera.points[0].x - camera.width / 2 <= 60 / 640
    assert camera.points[0].x + camera.width / 2 >= (60 + 180 * 29 / 15 + 100) / 640
    assert len({round(p.width or camera.width, 6) for p in camera.points}) == 1
    assert camera.points[0].y == 0.5
    blank = np.full((360, 640, 3), 255, np.uint8)
    assert drawing_box(blank) is None
    capture = cv2.VideoCapture(str(video))
    ok, frame = capture.read()
    capture.release()
    assert ok
    preview = camera_preview(frame, camera, 0, 360, 640)
    ys, xs = np.where((preview[:, :, 2] > 180) & (preview[:, :, 0] < 80))
    assert xs.min() > 10 and xs.max() < 350 and ys.min() > 10 and ys.max() < 630
    output = render(root, project, plan, tmp_path / "tracked.mp4", encoder="libx264")
    verify_output(output, 30, 15)
    capture = cv2.VideoCapture(str(output))
    checked = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            ys, xs = np.where((frame[:, :, 2] > 180) & (frame[:, :, 0] < 80))
            assert len(xs) > 1000
            assert xs.min() > 20 and xs.max() < 1060 and ys.min() > 20 and ys.max() < 1900
            checked += 1
    finally:
        capture.release()
    assert checked == 30


def test_photoshop_camera_reaches_canvas_edges_without_vertical_tracking() -> None:
    from sculptcut.photoshop import locked_camera

    canvas = Roi(x=0.1, y=0.1, width=0.8, height=0.8)
    for subject, edge in (
        (Roi(x=0.1, y=0.25, width=0.18, height=0.45), canvas.x),
        (Roi(x=0.72, y=0.25, width=0.18, height=0.45), canvas.x + canvas.width),
    ):
        camera = locked_camera([(0, subject)], 640, 360, 1.25, canvas)
        point = camera.points[0]
        assert point.y == canvas.y + canvas.height / 2
        assert camera.height == canvas.height
        assert camera.viewport == canvas
        if edge == canvas.x:
            assert abs(point.x - camera.width / 2 - edge) < 1e-9
        else:
            assert abs(point.x + camera.width / 2 - edge) < 1e-9
        assert point.x - camera.width / 2 <= subject.x
        assert point.x + camera.width / 2 >= subject.x + subject.width


def test_photoshop_detects_canvas_inside_interface() -> None:
    from sculptcut.photoshop import drawing_and_canvas_box, episode_canvas

    frame = np.full((360, 640, 3), 30, np.uint8)
    frame[35:325, 80:560] = (220, 220, 220)
    frame[90:270, 100:220] = (20, 60, 180)
    subject, visible_canvas = drawing_and_canvas_box(frame)
    assert subject is not None and visible_canvas is not None
    viewport = episode_canvas([visible_canvas], [subject], None)
    assert abs(viewport.x - 80 / 640) < 0.01
    assert abs(viewport.y - 35 / 360) < 0.01
    assert abs(viewport.width - 480 / 640) < 0.01
    assert abs(viewport.height - 290 / 360) < 0.01


def test_photoshop_export_limits_camera_to_canvas(tmp_path: Path) -> None:
    video = tmp_path / "canvas-inside-interface.mp4"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 15, (640, 360))
    assert writer.isOpened()
    for index in range(30):
        frame = np.full((360, 640, 3), 30, np.uint8)
        frame[35:325, 80:560] = (220, 220, 220)
        x = 95 + index // 10
        frame[90:270, x : x + 120] = (20, 60, 180)
        writer.write(frame)
    writer.release()

    root = tmp_path / "project"
    project = create_project(root, [video]).model_copy(
        update={"settings": ProjectSettings(preset="photoshop_shorts", output_fps=15)}
    )
    plan = plan_project(root, project, 60)
    camera = plan.decisions[0].subject_camera
    assert camera is not None
    assert abs(camera.viewport.x - 80 / 640) < 0.02
    assert abs(camera.viewport.y - 35 / 360) < 0.02
    assert abs(camera.points[0].y - 0.5) < 0.01
    assert abs(camera.points[0].x - (95 + 60) / 640) < 0.01
    assert camera.width < camera.viewport.width / 2
    output = render(root, project, plan, tmp_path / "canvas-short.mp4", encoder="libx264")
    verify_output(output, 30, 15)


def test_photoshop_preset_duration_and_switching() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.confirm_action = lambda *_: True  # type: ignore[method-assign]
    try:
        window.target.setValue(5)
        window.preset.setCurrentIndex(window.preset.findData("photoshop_shorts"))
        assert window.target.value() == 60
        assert window.target.isEnabled()
        assert window.target.seconds_mode and window.target.suffix() == ""
        assert window.output_suffix.text() == "_photoshop_shorts.mp4"
        window.preset.setCurrentIndex(window.preset.findData("shorts"))
        assert window.target.value() == 60 and window.target.isEnabled()
        window.preset.setCurrentIndex(window.preset.findData("timelapse"))
        assert window.target.value() == 5 and window.target.isEnabled()
    finally:
        window.close()
        app.processEvents()


def test_camera_bridges_lost_detection_and_rejects_single_frame_jumps() -> None:
    from sculptcut.photoshop import stable_camera

    samples = [
        (i * 100_000, Roi(x=0.15 + i * 0.005, y=0.2, width=0.2, height=0.6)) for i in range(60)
    ]
    damaged: list[tuple[int, Roi | None]] = list(samples)
    for i in range(20, 25):
        damaged[i] = (samples[i][0], None)
    damaged[40] = (samples[40][0], Roi(x=0.85, y=0.05, width=0.1, height=0.1))
    camera = stable_camera(damaged, 30, 640, 360, 1.25)
    for i, point in enumerate(camera.points):
        assert abs(point.x - (0.25 + i * 0.005)) < 0.006
        assert abs(point.y - 0.5) < 0.001
        assert point.width is not None and point.height is not None
    widths = np.array([p.width for p in camera.points], dtype=float)
    assert widths.max() / widths.min() < 1.01
    assert np.max(np.abs(np.diff([p.x for p in camera.points]))) < 0.012
    fallback = stable_camera([(0, None), (100_000, None)], 30, 640, 360, 1.25)
    assert fallback.width == fallback.height == 1


def test_camera_locks_small_detection_size_changes_to_one_scale() -> None:
    from sculptcut.photoshop import stable_camera

    samples = [
        (
            i * 100_000,
            Roi(
                x=0.3 + (0.004 if i % 2 else -0.004),
                y=0.2,
                width=0.2 * (1 + 0.04 * np.sin(i)),
                height=0.55 * (1 + 0.04 * np.sin(i)),
            ),
        )
        for i in range(60)
    ]
    camera = stable_camera(samples, 10, 640, 360, 1.25)
    widths = {round(float(point.width or 0), 9) for point in camera.points}
    heights = {round(float(point.height or 0), 9) for point in camera.points}
    assert len(widths) == len(heights) == 1
    assert max(point.x for point in camera.points) - min(point.x for point in camera.points) < 0.002


def test_gradient_canvas_does_not_become_the_tracked_subject() -> None:
    from sculptcut.photoshop import drawing_box

    frame = np.full((540, 960, 3), 30, np.uint8)
    gradient = np.linspace([170, 150, 135], [220, 200, 170], 480).astype(np.uint8)
    frame[30:510, 40:920] = gradient[:, None, :]
    frame[30:470, 390:460] = (35, 40, 50)
    frame[30:470, 490:560] = (35, 40, 50)
    box = drawing_box(frame)
    assert box is not None
    assert abs(box.x - 390 / 960) < 0.005
    assert abs(box.width - 170 / 960) < 0.005
    assert abs(box.height - 440 / 540) < 0.005


def test_faint_sketch_on_white_canvas_is_detected() -> None:
    from sculptcut.photoshop import drawing_box

    frame = np.full((360, 640, 3), 255, np.uint8)
    cv2.ellipse(frame, (340, 180), (70, 140), 0, 0, 360, (220, 220, 220), 2)
    box = drawing_box(frame)
    assert box is not None
    assert abs(box.x + box.width / 2 - 340 / 640) < 0.01
    assert 0.75 < box.height < 0.82


def test_zoomed_subject_is_not_mistaken_for_background() -> None:
    from sculptcut.photoshop import drawing_box

    frame = np.full((360, 640, 3), (190, 160, 130), np.uint8)
    frame[:, :400] = (65, 65, 65)
    box = drawing_box(frame)
    assert box is not None
    assert box.x == 0
    assert abs(box.width - 400 / 640) < 0.01
    assert box.height == 1


def test_photoshop_camera_preserves_source_zoom_without_adding_camera_motion(
    tmp_path: Path,
) -> None:
    from sculptcut.subject_camera import camera_preview

    video = tmp_path / "zoom-drawing.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=0xaaccdd:s=640x360:r=15:d=2",
            "-f",
            "lavfi",
            "-i",
            "color=c=0xff3300:s=60x120:r=15:d=2",
            "-filter_complex",
            "[1:v]scale=w='60+20*t':h='120+40*t':eval=frame[zoom];"
            "[0:v][zoom]overlay=x='80+80*t':y=70:shortest=1",
            "-c:v",
            "libx264",
            str(video),
        ]
    )
    root = tmp_path / "zoom-project"
    project = create_project(root, [video]).model_copy(
        update={"settings": ProjectSettings(preset="photoshop_shorts", output_fps=15)}
    )
    plan = plan_project(root, project, 60)
    assert len(plan.decisions) == 1
    camera = plan.decisions[0].subject_camera
    assert camera is not None and len(camera.points) == 1
    output = render(root, project, plan, tmp_path / "zoom-stable.mp4", encoder="libx264")
    verify_output(output, 30, 15)
    original, rendered = cv2.VideoCapture(str(video)), cv2.VideoCapture(str(output))
    heights = []
    try:
        for i in range(30):
            ok, frame = rendered.read()
            source_ok, source = original.read()
            assert ok and source_ok
            preview = camera_preview(source, camera, round(i * 1e6 / 15), 360, 640)
            for image in (frame, preview):
                ys, xs = np.where((image[:, :, 2] > 180) & (image[:, :, 0] < 80))
                h, w = image.shape[:2]
                assert ys.min() >= 0 and ys.max() < h
                assert xs.min() >= 0 and xs.max() < w
            heights.append((ys.max() - ys.min()) / h)
    finally:
        original.release()
        rendered.release()
    # Source zoom remains visible; the application applies one constant camera.
    assert heights[-1] - heights[0] > 0.1
