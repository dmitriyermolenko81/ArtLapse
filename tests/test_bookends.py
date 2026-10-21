"""Final presentation survives all delivery formats without changing chronology."""

from pathlib import Path

import cv2
import numpy as np
import pytest
from test_timeline import make_source

from sculptcut.bookends import final_frames, present_final_result
from sculptcut.domain import CameraPoint, Project, ProjectSettings, Roi, SubjectCamera
from sculptcut.media import discover, run_process
from sculptcut.planning import build_plan, validate_plan
from sculptcut.project import create_project
from sculptcut.rendering import render, verify_output
from sculptcut.shorts_layout import apply_square_layout


@pytest.mark.parametrize(
    "preset", ["timelapse", "shorts", "beat_shorts", "photoshop_shorts", "photoshop_beat_shorts"]
)
@pytest.mark.parametrize("layout", ["auto", "square"])
def test_final_hold_and_zoom_share_all_layouts(preset: str, layout: str) -> None:
    project = Project(
        sources=[make_source(0, 0, 60_000_000)],
        settings=ProjectSettings(
            preset=preset,
            shorts_layout=layout,
            shorts_square_rois={"0": Roi(x=0, y=0, width=1, height=1)},
        ),
    )
    base = build_plan(project, [], 30)
    d = base.decisions[0]
    plan = base.model_copy(
        update={
            "decisions": [
                d.model_copy(update={"source_end_us": 59_600_000, "output_frame_count": 888}),
                d.model_copy(
                    update={
                        "source_start_us": 59_600_000,
                        "action": "KEEP",
                        "output_frame_count": 12,
                        "provenance": "user:final_tail",
                    }
                ),
            ]
        }
    )
    result = present_final_result(project, apply_square_layout(project, plan))
    validate_plan(project, result)
    assert result.actual_frame_count == 900
    assert final_frames(result) == 120
    assert result.decisions[-1].action == "HOLD"
    assert result.decisions[-1].source_start_us == 59_600_000
    assert present_final_result(project, result) == result


def test_beat_final_hold_keeps_every_music_cut_and_excluded_source_tail() -> None:
    project = Project(
        sources=[make_source(0, 0, 10_000_000)], settings=ProjectSettings(preset="beat_shorts")
    )
    base = build_plan(project, [], 6)
    d = base.decisions[0]
    parts = [
        d.model_copy(
            update={
                "source_start_us": i * 1_000_000,
                "source_end_us": (i + 1) * 1_000_000,
                "action": "HOLD",
                "provenance": "auto:beat_still",
                "output_frame_count": n,
                "subject_camera": SubjectCamera(
                    viewport=Roi(x=0, y=0, width=1, height=1),
                    width=1,
                    height=1,
                    points=[CameraPoint(time_us=0, x=0.5, y=0.5)],
                    background=0,
                ),
            }
        )
        for i, n in enumerate([15, 30, 45, 90])
    ]
    parts.append(
        d.model_copy(
            update={
                "source_start_us": 4_000_000,
                "action": "DROP",
                "output_frame_count": 0,
                "provenance": "user:DROP",
            }
        )
    )
    plan = base.model_copy(
        update={"decisions": parts, "settings": base.settings | {"story": "beat_stills"}}
    )
    result = present_final_result(project, plan)
    assert [x.output_frame_count for x in result.decisions] == [15, 30, 45, 90, 0]
    assert result.decisions[-1] == parts[-1]
    assert final_frames(result) == 90
    validate_plan(project, result)


def test_unlabelled_long_final_clip_keeps_body_in_motion() -> None:
    project = Project(sources=[make_source(0, 0, 60_000_000)], settings=ProjectSettings())
    plan = build_plan(project, [], 30)
    result = present_final_result(project, plan)
    assert result.actual_frame_count == 900
    assert len(result.decisions) == 2
    assert result.decisions[0].action == "SPEED"
    assert result.decisions[0].output_frame_count == 780
    assert result.decisions[1].action == "HOLD"
    assert result.decisions[1].output_frame_count == 120
    assert final_frames(result) == 120
    validate_plan(project, result)


@pytest.mark.parametrize("square", [False, True])
def test_final_zoom_real_export(tmp_path: Path, square: bool) -> None:
    video = tmp_path / "source.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x320:r=10:d=10,drawbox=x=110:y=120:w=100:h=80:color=white:t=fill",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            str(video),
        ]
    )
    root = tmp_path / "project"
    project = create_project(root, [video])
    sid = project.sources[0].id
    project = project.model_copy(
        update={
            "settings": ProjectSettings(
                preset="shorts" if square else "timelapse",
                output_fps=10,
                shorts_layout="square" if square else "auto",
                shorts_square_rois={sid: Roi(x=0, y=0, width=1, height=1)},
            )
        }
    )
    base = build_plan(project, [], 10)
    d = base.decisions[0]
    plan = base.model_copy(
        update={
            "decisions": [
                d.model_copy(update={"source_end_us": 9_800_000, "output_frame_count": 98}),
                d.model_copy(
                    update={
                        "source_start_us": 9_800_000,
                        "action": "KEEP",
                        "output_frame_count": 2,
                        "provenance": "user:final_tail",
                    }
                ),
            ]
        }
    )
    plan = present_final_result(project, apply_square_layout(project, plan))
    output = render(root, project, plan, tmp_path / "final.mp4", encoder="libx264")
    verify_output(output, 100, 10)
    capture = cv2.VideoCapture(str(output))
    widths = []
    try:
        for index in [0, 80, 99]:
            capture.set(cv2.CAP_PROP_POS_FRAMES, index)
            ok, frame = capture.read()
            assert ok
            mask = np.all(frame > 220, axis=2)
            ys, xs = np.where(mask)
            assert len(xs)
            widths.append(int(xs.max() - xs.min() + 1))
            if square:
                assert frame[:100].mean() < 35 and frame[-100:].mean() < 35
    finally:
        capture.release()
    assert abs(widths[0] - widths[1]) <= 2
    assert widths[2] > widths[1] + 3


def test_final_hold_uses_last_selected_frame_not_early_work(tmp_path: Path) -> None:
    video = tmp_path / "red-blue.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=red:s=160x120:r=10:d=1[r];color=blue:s=160x120:r=10:d=1[b];[r][b]concat=n=2:v=1:a=0",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            str(video),
        ]
    )
    root = tmp_path / "project"
    project = create_project(root, [video])
    project = project.model_copy(update={"settings": ProjectSettings(output_fps=10)})
    base = build_plan(project, [], 1)
    plan = base.model_copy(
        update={
            "decisions": [
                base.decisions[0].model_copy(
                    update={
                        "action": "HOLD",
                        "provenance": "user:final_tail",
                    }
                )
            ]
        }
    )
    output = render(root, project, plan, tmp_path / "final.mp4", encoder="libx264")
    verify_output(output, 10, 10)
    reader = cv2.VideoCapture(str(output))
    try:
        ok, frame = reader.read()
        assert ok
        assert frame[:, :, 0].mean() > frame[:, :, 2].mean() * 2
    finally:
        reader.release()
