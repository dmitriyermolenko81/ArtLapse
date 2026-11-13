"""A final presentation uses a clean result, excluding manually removed frames."""

from pathlib import Path

import cv2
import numpy as np
import pytest
from test_beat_shorts import pulse_track

from sculptcut.combined_edits import apply_episode_edits
from sculptcut.domain import Override, ProjectSettings, Roi
from sculptcut.final_result import ensure_final_result, presentation_crop, result_frame
from sculptcut.planning import RenderDecision, build_plan, validate_plan
from sculptcut.project import create_project
from sculptcut.proxy import make_proxies
from sculptcut.rendering import render, verify_output
from sculptcut.shorts_layout import apply_square_layout
from sculptcut.variants import read_variant


def image(menu: bool = False, cutoff: bool = False) -> np.ndarray:
    frame = np.full((360, 640, 3), 20, np.uint8)
    frame[40:320, 80:560] = 70
    cv2.ellipse(frame, (90 if cutoff else 320, 185), (60, 110), 0, 0, 360, (20, 210, 20), -1)
    if menu:
        frame[90:310, 280:390] = 90
        for row in range(105, 300, 12):
            cv2.putText(
                frame, "Item", (288, row), cv2.FONT_HERSHEY_SIMPLEX, 0.25, (180, 180, 180), 1
            )
    return frame


def test_final_image_rejects_popup_and_clipped_model_but_accepts_clean_result() -> None:
    assert result_frame(image()) is not None
    assert result_frame(image(menu=True)) is None
    assert result_frame(image(cutoff=True)) is None


@pytest.mark.parametrize("preset", ["timelapse", "shorts", "music_shorts"])
def test_real_clean_final_replaces_menu_without_returning_manual_cut_or_changing_time(
    tmp_path: Path,
    preset: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "result.mp4"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 30, (640, 360))
    assert writer.isOpened()
    for index in range(240):
        frame = image(menu=60 <= index < 120)
        if index >= 210:
            frame[:, :, 0], frame[:, :, 1] = frame[:, :, 1].copy(), frame[:, :, 0].copy()
        writer.write(frame)
    writer.release()
    music = tmp_path / "music.wav"
    pulse_track(music, [0.5, 1, 1.5, 2, 2.5], seconds=3)
    root = tmp_path / "project"
    project = create_project(root, [video])
    sid = project.sources[0].id
    project = project.model_copy(
        update={
            "settings": ProjectSettings.model_validate(
                {
                    "preset": preset,
                    "shorts_layout": "square" if preset != "timelapse" else "auto",
                    "shorts_square_rois": {
                        sid: Roi(x=180 / 640, y=40 / 360, width=280 / 640, height=280 / 360)
                    },
                    "proxy_width": 640,
                    "proxy_fps": 5,
                    "target_seconds": 3,
                    "music_path": music,
                }
            ),
            "overrides": [
                Override(source_id=sid, start_us=7_000_000, end_us=8_000_000, action="DROP")
            ],
        }
    )
    make_proxies(root, project)
    base = build_plan(project, [], 3)
    base = base.model_copy(
        update={
            "decisions": [
                RenderDecision(
                    source_id=sid,
                    source_start_us=a * 1_000_000,
                    source_end_us=b * 1_000_000,
                    action=action,
                    output_frame_count=count,
                    provenance=provenance,
                )
                for a, b, action, count, provenance in (
                    (0, 2, "SPEED", 60, "auto:episode"),
                    (2, 4, "HOLD", 30, "user:final_tail"),
                    (4, 7, "DROP", 0, "story:episode_omission"),
                    (7, 8, "DROP", 0, "user:shorts_manual_cut"),
                )
            ],
            "settings": base.settings
            | {"bookends": True, "final_view_frames": 30, "width": 180, "height": 320},
        }
    )
    base = apply_square_layout(project, base)
    validate_plan(project, base)
    fixed = apply_episode_edits(project, base, root=root)
    tail = next(d for d in reversed(fixed.decisions) if d.output_frame_count)
    assert (
        tail.action == "HOLD"
        and 4_000_000 <= tail.source_start_us < tail.source_end_us <= 7_000_000
    )
    assert tail.output_frame_count == 30 and fixed.actual_frame_count == 90
    assert ensure_final_result(root, project, fixed) == fixed
    assert all(not d.output_frame_count or d.source_end_us <= 7_000_000 for d in fixed.decisions)
    monkeypatch.setattr("sculptcut.rendering.export_size", lambda *args: (180, 320))
    output = render(root, project, fixed, tmp_path / "fixed.mp4", encoder="libx264")
    verify_output(output, 90, 30, expected_audio=True)
    saved = read_variant(output)
    assert (
        apply_episode_edits(project, type(fixed).model_validate(saved["plan"]), root=root) == fixed
    )
    reader = cv2.VideoCapture(str(output))
    try:
        reader.set(cv2.CAP_PROP_POS_FRAMES, 80)
        ok, frame = reader.read()
        assert ok
        green = (
            (frame[:, :, 1] > 120)
            & (frame[:, :, 1] > frame[:, :, 0] * 1.5)
            & (frame[:, :, 1] > frame[:, :, 2] * 1.5)
        )
        assert np.count_nonzero(green) > 1000
    finally:
        reader.release()


def test_timelapse_final_crop_fills_frame_with_margins_and_keeps_aspect():
    view = Roi(x=0.1, y=0.1, width=0.8, height=0.8)
    model = Roi(x=0.4, y=0.35, width=0.2, height=0.4)
    crop = presentation_crop(view, model, 1920, 1080, 16/9)
    assert crop is not None
    assert abs(crop.width*1920/(crop.height*1080)-16/9) < 1e-6
    assert crop.x <= model.x and crop.y <= model.y
    assert crop.x+crop.width >= model.x+model.width
    assert crop.y+crop.height >= model.y+model.height
    assert 0.65 < model.height/crop.height <= 0.76+1e-9
    assert crop.x >= view.x and crop.y >= view.y
    assert crop.x+crop.width <= view.x+view.width
    assert crop.y+crop.height <= view.y+view.height
    wide = Roi(x=0.12,y=0.2,width=0.76,height=0.6)
    assert presentation_crop(view,wide,1920,1080,9/16) is None
    edge = Roi(x=0.101, y=0.35, width=0.2, height=0.4)
    assert presentation_crop(view,edge,1920,1080,16/9) is None
