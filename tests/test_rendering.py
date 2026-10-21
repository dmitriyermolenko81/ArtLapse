import hashlib
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from threading import Event

import cv2
import pytest
from test_timeline import make_source

from sculptcut.domain import Project, ProjectSettings
from sculptcut.idle import IdleRange
from sculptcut.media import Cancelled, MediaError, discover, probe, run_process
from sculptcut.planning import build_plan, validate_plan
from sculptcut.project import create_project, save_project
from sculptcut.rendering import chunk_decisions, normalize_timing, render, verify_output


def test_plan_budget_coverage_and_impossible_target() -> None:
    project = Project(sources=[make_source(0, 0, 10_000_000)])
    idle = [IdleRange(source_id="0", start_us=2_000_000, end_us=7_000_000)]
    plan = build_plan(project, idle, 2)
    assert plan.actual_frame_count == 60
    assert [d.action for d in plan.decisions] == ["SPEED", "DROP", "SPEED"]
    assert build_plan(project, idle, 20).warnings
    with pytest.raises(ValueError):
        validate_plan(project, plan.model_copy(update={"actual_frame_count": 1}))
    with pytest.raises(ValueError):
        build_plan(project, idle, float("nan"))


def test_chunk_boundaries_follow_the_camera_sampling_grid() -> None:
    project = Project(sources=[make_source(0, 0, 721_000_000)])
    plan = build_plan(project, [], 119 / 30)
    pieces = chunk_decisions(plan)
    assert len(pieces) == 3
    frame = 0
    for piece in pieces:
        assert piece.source_start_us == 721_000_000 * frame // 119
        frame += piece.output_frame_count
        assert piece.source_end_us == 721_000_000 * frame // 119
    assert frame == 119


def test_normalize_keeps_clock_across_color_range_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sculptcut.rendering as rendering

    clips = []
    for color_range, color in (("tv", "red"), ("pc", "blue")):
        clip = tmp_path / f"{color_range}.mp4"
        run_process(
            [
                discover("ffmpeg"),
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=160x120:r=30:d=1",
                "-vf",
                "format=yuv420p",
                "-color_range",
                color_range,
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                str(clip),
            ]
        )
        clips.append(clip)

    listing = tmp_path / "concat.txt"
    listing.write_text(
        "".join(f"file '{clips[index % 2].name}'\n" for index in range(12)),
        encoding="utf-8",
    )
    combined = tmp_path / "combined.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "concat",
            "-safe",
            "1",
            "-i",
            str(listing),
            "-c",
            "copy",
            str(combined),
        ]
    )
    monkeypatch.setattr(rendering, "doctor", lambda: {"h264_nvenc": False})
    output = tmp_path / "normalized.mp4"
    normalize_timing(combined, output, 30)
    verify_output(output, 360, 30)


def test_shorts_preview_and_export_play_every_episode_in_order(tmp_path: Path) -> None:
    from sculptcut.proxy import make_proxies

    clips = []
    for color in ("red", "lime", "blue"):
        clip = tmp_path / f"{color}.mp4"
        run_process(
            [
                discover("ffmpeg"),
                "-v", "error", "-f", "lavfi", "-i",
                f"color=c={color}:s=160x120:r=30:d=2",
                "-c:v", "libx264", "-preset", "ultrafast", str(clip),
            ]
        )
        clips.append(clip)
    listing = tmp_path / "concat.txt"
    listing.write_text("".join(f"file '{clip.name}'\n" for clip in clips), encoding="utf-8")
    source = tmp_path / "source.mp4"
    run_process(
        [
            discover("ffmpeg"), "-v", "error", "-f", "concat", "-safe", "1",
            "-i", str(listing), "-c", "copy", str(source),
        ]
    )
    root = tmp_path / "project"
    project = create_project(root, [source]).model_copy(
        update={"settings": ProjectSettings(preset="shorts", proxy_width=128, proxy_fps=10)}
    )
    make_proxies(root, project)
    base = build_plan(project, [], 3)
    original = base.decisions[0]
    decisions = [
        original.model_copy(
            update={
                "source_start_us": index * 2_000_000,
                "source_end_us": (
                    project.sources[0].duration_us if index == 2 else (index + 1) * 2_000_000
                ),
                "output_frame_count": 30,
                "provenance": "user:process_story",
            }
        )
        for index in range(3)
    ]
    plan = base.model_copy(
        update={
            "decisions": decisions,
            "settings": base.settings | {"story": "process", "bookends": False},
        }
    )
    for preview in (True, False):
        output = render(
            root, project, plan, tmp_path / f"shorts-{preview}.mp4",
            preview=preview, encoder="libx264",
        )
        verify_output(output, 90, 30)
        reader = cv2.VideoCapture(str(output))
        try:
            for index, channel in ((15, 2), (45, 1), (75, 0)):
                reader.set(cv2.CAP_PROP_POS_FRAMES, index)
                ok, frame = reader.read()
                assert ok
                center = frame[frame.shape[0] // 2, frame.shape[1] // 2]
                assert center[channel] > 150
                assert all(center[other] < 80 for other in range(3) if other != channel)
        finally:
            reader.release()


def test_real_cli_end_to_end(tmp_path: Path) -> None:
    sources = []
    for index, color in enumerate(("red", "blue")):
        path = tmp_path / f"исходник {index}.mp4"
        run_process(
            [
                discover("ffmpeg"),
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=160x120:r=10:d=8",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=8",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                "-shortest",
                str(path),
            ]
        )
        sources.append(path)
    hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in sources]
    root = tmp_path / "проект"
    project = create_project(root, sources).model_copy(
        update={"settings": ProjectSettings(proxy_width=160, proxy_fps=5)}
    )
    save_project(root, project)
    output = tmp_path / "готовый результат.mp4"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "sculptcut",
            "run",
            str(root),
            "--target-seconds",
            "2",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    verify_output(output, 60, 30)
    assert probe(output).audio_streams == []
    assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in sources] == hashes
    plan = json.loads((root / "render_plan.json").read_text())
    assert any(d["action"] == "DROP" for d in plan["decisions"])
    preview_output = tmp_path / "preview.mp4"
    before_features = (root / "logs" / "features.jsonl").read_bytes()
    preview_run = subprocess.run(
        [sys.executable, "-m", "sculptcut", "preview", str(root), "--output", str(preview_output)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert preview_run.returncode == 0, preview_run.stderr
    verify_output(preview_output, 60, 30)
    assert (root / "logs" / "features.jsonl").read_bytes() == before_features
    assert (root / "review.html").is_file()
    for arguments in (
        ["override", str(root), "source-0000", "0", "0.2", "KEEP"],
        ["plan", str(root), "--target-seconds", "2"],
    ):
        reviewed = subprocess.run(
            [sys.executable, "-m", "sculptcut", *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        assert reviewed.returncode == 0, reviewed.stderr
    assert (root / "logs" / "features.jsonl").read_bytes() == before_features
    revised = json.loads((root / "render_plan.json").read_text())
    assert revised["decisions"][0]["action"] == "KEEP"

    capture = cv2.VideoCapture(str(output))
    try:
        ok, first = capture.read()
        assert ok and first[:, :, 2].mean() > first[:, :, 0].mean() * 2
        capture.set(cv2.CAP_PROP_POS_FRAMES, 59)
        ok, last = capture.read()
        assert ok and last[:, :, 0].mean() > last[:, :, 2].mean() * 2
    finally:
        capture.release()


def test_chunks_cover_long_source() -> None:
    project = Project(sources=[make_source(0, 0, 18_000_000_000)])
    plan = build_plan(project, [], 600)
    chunks = chunk_decisions(plan)
    assert sum(c.output_frame_count for c in chunks) == 18000
    assert all(c.source_end_us - c.source_start_us <= 300_000_000 for c in chunks)
    assert all(c.output_frame_count <= 900 for c in chunks)
    assert chunks[0].source_start_us == 0 and chunks[-1].source_end_us == 18_000_000_000


def test_render_fallback_cancel_and_hold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sculptcut.rendering as renderer

    source = tmp_path / "offset.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=160x120:r=15:d=2",
            "-c:v",
            "libx264",
            "-output_ts_offset",
            "3",
            str(source),
        ]
    )
    root = tmp_path / "project"
    project = create_project(root, [source])
    plan = build_plan(project, [], 1)
    monkeypatch.setattr(renderer, "doctor", lambda: {"h264_nvenc": True})
    attempts = []

    def fail_gpu(
        args: Sequence[str],
        cancel: Event | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> str:
        if "h264_nvenc" in args:
            attempts.append("gpu")
            raise MediaError("Injected GPU failure")
        return run_process(args, cancel, progress)

    monkeypatch.setattr(renderer, "run_process", fail_gpu)
    progress: list[float] = []
    output = render(root, project, plan, tmp_path / "fallback.mp4", progress=progress.append)
    assert attempts and progress[-1] == 1
    verify_output(output, 30, 30)
    event = Event()
    event.set()
    with pytest.raises(Cancelled):
        render(root, project, plan, tmp_path / "cancelled.mp4", event, encoder="libx264")
    assert not (tmp_path / "cancelled.mp4").exists()
    assert not list(tmp_path.glob("sculptcut-render-*"))
    hold = plan.model_copy(
        update={
            "decisions": [
                plan.decisions[0].model_copy(
                    update={"action": "HOLD", "provenance": "user:verified_frame"}
                )
            ]
        }
    )
    verify_output(render(root, project, hold, tmp_path / "hold.mp4", encoder="libx264"), 30, 30)


def test_shorts_finale_does_not_copy_last_frame_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sculptcut.rendering as renderer

    source = tmp_path / "chronological.mp4"
    clips = {}
    for color in ("red", "blue"):
        clips[color] = tmp_path / f"{color}.mp4"
        run_process(
            [
                discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                f"color=c={color}:s=160x120:r=30:d=0.5", "-c:v", "libx264",
                "-preset", "ultrafast", str(clips[color]),
            ]
        )
    listing = tmp_path / "concat.txt"
    listing.write_text(
        "".join(
            f"file '{clips['red'].name if i < 4 else clips['blue'].name}'\n"
            for i in range(10)
        ),
        encoding="utf-8",
    )
    run_process(
        [discover("ffmpeg"), "-v", "error", "-f", "concat", "-safe", "1", "-i",
         str(listing), "-c", "copy", str(source)]
    )
    project = Project(
        sources=[make_source(0, 0, 5_000_000)],
        settings=ProjectSettings(preset="shorts"),
    )
    plan = build_plan(project, [], 5)
    original = plan.decisions[0]
    plan = plan.model_copy(
        update={
            "settings": plan.settings | {"bookends": True},
            "decisions": [
                original.model_copy(
                    update={
                        "source_end_us": 2_000_000,
                        "output_frame_count": 60,
                        "provenance": "user:process_story",
                    }
                ),
                original.model_copy(
                    update={
                        "source_start_us": 2_000_000,
                        "output_frame_count": 90,
                        "provenance": "user:shorts_final",
                    }
                ),
            ],
        }
    )
    monkeypatch.setattr(renderer, "doctor", lambda: {"h264_nvenc": False})
    output = tmp_path / "chronological-short.mp4"
    normalize_timing(source, output, 30, bookend_plan=plan)
    verify_output(output, 150, 30)

    capture = cv2.VideoCapture(str(output))
    try:
        ok, first = capture.read()
        assert ok and first[:, :, 2].mean() > first[:, :, 0].mean() * 2
        capture.set(cv2.CAP_PROP_POS_FRAMES, 149)
        ok, last = capture.read()
        assert ok and last[:, :, 0].mean() > last[:, :, 2].mean() * 2
    finally:
        capture.release()


def test_montage_real_export(tmp_path: Path) -> None:
    source = tmp_path / "states.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=160x120:r=15:d=2",
            "-c:v",
            "libx264",
            str(source),
        ]
    )
    root = tmp_path / "project"
    project = create_project(root, [source])
    plan = build_plan(project, [], 1)
    decision = plan.decisions[0].model_copy(
        update={"action": "MONTAGE", "sample_times_us": [0, 1000000]}
    )
    plan = plan.model_copy(update={"decisions": [decision]})
    output = render(root, project, plan, tmp_path / "montage.mp4", encoder="libx264")
    verify_output(output, 30, 30)
