from pathlib import Path
from threading import Event

import cv2
import numpy as np
import pytest
from numpy.typing import NDArray

from sculptcut.domain import Roi
from sculptcut.project import create_project
from sculptcut.proxy import proxy_path
from sculptcut.shorts_quality import EpisodeInspector, proxy_sample, stable_episode_score


def test_forward_samples_match_seeking_and_cancel(tmp_path: Path) -> None:
    from sculptcut.media import Cancelled, discover, run_process

    source = tmp_path / "cfr.mp4"
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 "testsrc2=s=128x96:r=5:d=4", "-c:v", "libx264", str(source)])
    sequential, reference = cv2.VideoCapture(str(source)), cv2.VideoCapture(str(source))
    try:
        for timestamp in (0, 100_000, 100_001, 500_000, 1_200_000,
                          2_900_000, 100_000, 2_000_000, 3_700_000):
            reference.set(cv2.CAP_PROP_POS_MSEC, timestamp / 1000)
            ok, frame = reference.read()
            assert ok
            sampled = proxy_sample(sequential, timestamp, 5)
            assert sampled is not None
            np.testing.assert_array_equal(sampled, frame)
        cancelled = Event()
        cancelled.set()
        with pytest.raises(Cancelled):
            proxy_sample(sequential, 3_800_000, 5, cancelled)
    finally:
        sequential.release()
        reference.release()


def sculpt_frame(cx: int = 320, radius: int = 80) -> NDArray[np.uint8]:
    frame = np.full((480, 640, 3), 20, dtype=np.uint8)
    cv2.rectangle(frame, (65, 40), (575, 440), (70, 70, 70), -1)
    cv2.ellipse(frame, (cx, 235), (radius, 135), 0, 0, 360, (150, 150, 150), -1)
    return frame


def test_visual_selection_rejects_pan_zoom_and_clipped_subject() -> None:
    assert stable_episode_score([sculpt_frame() for _ in range(9)]) > 0.9
    assert stable_episode_score([sculpt_frame(250 + i * 20) for i in range(9)]) == 0
    assert stable_episode_score([sculpt_frame(radius=50 + i * 10) for i in range(9)]) == 0
    assert stable_episode_score([sculpt_frame(70) for _ in range(9)]) == 0


def test_visual_selection_accepts_bust_continuing_below_canvas() -> None:
    frame = sculpt_frame()
    cv2.rectangle(frame, (295, 320), (345, 440), (150, 150, 150), -1)
    assert stable_episode_score([frame for _ in range(9)]) > 0.9


def test_visual_selection_clamps_bottom_edge_rounding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sculptcut.shorts_quality.screen_kind", lambda gray: None)
    monkeypatch.setattr(
        "sculptcut.shorts_quality.viewport_box",
        lambda gray: Roi(x=0.1, y=0.1, width=0.8, height=0.8),
    )
    monkeypatch.setattr(
        "sculptcut.shorts_quality.sculpt_footprint",
        lambda gray, viewport: Roi(x=0.3, y=0.3, width=0.2, height=0.6000000000000001),
    )
    assert stable_episode_score([sculpt_frame() for _ in range(9)]) > 0.9


def test_real_proxy_inspection_prefers_continuous_brush_work(tmp_path: Path) -> None:
    video = tmp_path / "work.mp4"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 10, (640, 480))
    for i in range(60):
        frame = sculpt_frame(240 + i * 5 if i < 30 else 320)
        cv2.circle(frame, (320 + i % 10, 220), 5, (100, 100, 100), -1)
        writer.write(frame)
    writer.release()
    root = tmp_path / "project"
    project = create_project(root, [video])
    source = project.sources[0]
    proxy = proxy_path(root, project, source)
    proxy.parent.mkdir(exist_ok=True)
    proxy.write_bytes(video.read_bytes())
    with EpisodeInspector(root, project) as inspector:
        assert inspector.score(source.id, 0, 3_000_000) == 0
        assert inspector.score(source.id, 3_000_000, 6_000_000) > 0.8
