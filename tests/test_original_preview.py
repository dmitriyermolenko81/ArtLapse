"""Compare generated full-HD review media, decoding and interactive seeks."""

import json
from pathlib import Path
from threading import Event
from time import monotonic

import cv2
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
from PySide6.QtWidgets import QApplication, QWidget

from sculptcut import rendering
from sculptcut.domain import ProjectSettings
from sculptcut.episode_images import image_key
from sculptcut.media import discover, run_process
from sculptcut.planning import RenderDecision, build_plan
from sculptcut.project import create_project, save_project
from sculptcut.quick_seek import build_copy
from sculptcut.rendering import render, verify_output
from sculptcut.scrub_frames import ScrubFrames
from sculptcut.variants import read_variant


def wait(app, predicate, seconds=15):
    deadline = monotonic() + seconds
    while not predicate() and monotonic() < deadline:
        loop = QEventLoop()
        QTimer.singleShot(2, loop.quit)
        loop.exec()
    assert predicate(), "timeout"


def test_original_preview_resolution_quality_and_performance(tmp_path):
    app = QApplication.instance() or QApplication([])
    source = tmp_path / "source.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1920x1080:rate=30:duration=12",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            str(source),
        ]
    )
    root = tmp_path / "project"
    project = create_project(root, [source])
    project = project.model_copy(
        update={
            "settings": ProjectSettings(
                target_seconds=6, final_view_seconds=0, proxy_width=640, proxy_fps=15
            )
        }
    )
    save_project(root, project)
    plan = build_plan(project, [], 6)
    parts = [
        RenderDecision(
            source_id=project.sources[0].id,
            source_start_us=i * 1_000_000,
            source_end_us=(i + 1) * 1_000_000,
            action="SPEED",
            output_frame_count=15,
            provenance="story:work",
        )
        for i in range(12)
    ]
    plan = plan.model_copy(update={"decisions": parts})
    results = {}
    try:
        for high in (False, True):
            rendering.ORIGINAL_QUALITY_PREVIEW = high
            video = root / "previews" / ("original.mp4" if high else "fast.mp4")
            start = monotonic()
            render(root, project, plan, video, Event(), preview=True, encoder="libx264")
            render_seconds = monotonic() - start
            verify_output(video, 180, 30)
            cap = cv2.VideoCapture(str(video))
            size = (round(cap.get(3)), round(cap.get(4)))
            cap.release()
            assert size == ((1920, 1080) if high else (640, 360))
            assert read_variant(video)["plan"]["settings"]["draft"] == 1
            if high:
                assert read_variant(video)["plan"]["settings"]["preview_quality"] == "original"
            start = monotonic()
            copy = build_copy(
                video, tmp_path / ("hq-seek.mp4" if high else "fast-seek.mp4"), Event()
            )
            cache_seconds = monotonic() - start
            owner = QWidget()
            frames = ScrubFrames(owner)
            frames.worker.use_proxy(video.resolve(), copy)
            received = []
            frames.ready.connect(
                lambda key, image, received=received: received.append((key, monotonic()))
            )
            latencies = []
            try:
                for index in (150, 12, 90, 45, 165, 30, 120, 75):
                    key = image_key(video, index * 1_000_000 // 30)
                    received.clear()
                    start = monotonic()
                    frames.request(key)
                    wait(app, lambda received=received: bool(received))
                    latencies.append((received[-1][1] - start) * 1000)
            finally:
                frames.shutdown()
                wait(app, lambda frames=frames: frames.cleaned)
                owner.close()
            player = QMediaPlayer()
            sink = QVideoSink()
            audio = QAudioOutput()
            player.setAudioOutput(audio)
            player.setVideoSink(sink)
            stamps = []
            sink.videoFrameChanged.connect(
                lambda frame, stamps=stamps: stamps.append(monotonic()) if frame.isValid() else None
            )
            from PySide6.QtCore import QUrl

            player.setSource(QUrl.fromLocalFile(str(video)))
            start = monotonic()
            player.play()
            wait(
                app,
                lambda player=player: player.mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia,
            )
            playback_seconds = monotonic() - start
            player.stop()
            player.setSource(QUrl())
            assert len(stamps) > 140
            gaps = [(b - a) * 1000 for a, b in zip(stamps, stamps[1:], strict=False)]
            results["original" if high else "fast"] = {
                "size": size,
                "render_seconds": render_seconds,
                "file_bytes": video.stat().st_size,
                "seek_copy_seconds": cache_seconds,
                "seek_copy_bytes": copy.stat().st_size,
                "seek_ms": latencies,
                "seek_max_ms": max(latencies),
                "play_frames": len(stamps),
                "play_seconds": playback_seconds,
                "frame_gap_max_ms": max(gaps),
            }
    finally:
        rendering.ORIGINAL_QUALITY_PREVIEW = True
    out = Path("artifacts/original-preview-307")
    out.mkdir(parents=True, exist_ok=True)
    (out / "benchmark.json").write_text(json.dumps(results, indent=2), encoding="utf8")
    print(json.dumps(results))
