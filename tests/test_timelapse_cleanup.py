from pathlib import Path
from threading import Event

import cv2
import numpy as np
import pytest

from sculptcut.media import Cancelled
from sculptcut.timelapse_cleanup import appearance, camera_only, signature, stable


def model_pair():
    previous = np.full((320, 512), 70, dtype=np.uint8)
    texture = np.random.default_rng(42).integers(80, 190, (140, 120), dtype=np.uint8)
    previous[130:270, 190:310] = texture
    matrix = cv2.getRotationMatrix2D((250, 200), 0, 1.18)
    matrix[0, 2] += 12
    current = cv2.warpAffine(previous, matrix, (512, 320), borderValue=70)
    for image in (previous, current):
        image[:30] = 35
        image[:, :65] = 45
        image[:, -45:] = 45
    return previous, current


def test_zoom_with_static_panels_is_removed_but_local_edits_are_preserved() -> None:
    previous, current = model_pair()
    before = signature(previous)
    assert camera_only(previous, current, before, signature(current))
    edited = current.copy()
    cv2.circle(edited, (250, 200), 12, 20, -1)
    assert not camera_only(previous, edited, before, signature(edited))
    assert not camera_only(previous, current, (before[0], None), signature(current))


def test_camera_cleanup_keeps_short_movements_and_context_without_bridging_edits() -> None:
    from sculptcut.idle import IdleRange
    from sculptcut.timelapse_cleanup import conservative_camera_ranges

    def camera(start: int, end: int) -> IdleRange:
        return IdleRange(source_id="a", start_us=start, end_us=end,
                         reason="timelapse_camera_only")

    assert conservative_camera_ranges([camera(0, 200_000)]) == []
    # An uncertain/editing gap must not join two brief camera movements.
    assert conservative_camera_ranges([camera(0, 600_000), camera(800_000, 1_400_000)]) == []
    result = conservative_camera_ranges([camera(0, 600_000), camera(600_000, 1_400_000)])
    assert [(r.start_us, r.end_us) for r in result] == [(400_000, 1_000_000)]
    other = IdleRange(source_id="a", start_us=2_000_000, end_us=5_000_000,
                      reason="timelapse_static_view")
    assert conservative_camera_ranges([other]) == [other]


def test_static_image_with_animated_gradient_and_small_edits() -> None:
    previous, current = model_pair()
    assert stable(appearance(previous), appearance(previous.copy()))
    edited = previous.copy()
    edited[180:190, 240:250] = 255
    assert not stable(appearance(previous), appearance(edited))
    height, width = previous.shape
    gradient = np.repeat(np.linspace(40, 100, height)[:, None], width, axis=1).astype(np.uint8)
    changed = np.repeat(np.linspace(100, 140, height)[:, None], width, axis=1).astype(np.uint8)
    assert stable(appearance(gradient), appearance(changed))
    gradient[160:190, 220:250] = previous[160:190, 220:250]
    changed[160:190, 220:250] = gradient[160:190, 220:250]
    # A foreground contrast change is ambiguous; do not infer inactivity.
    assert not stable(appearance(gradient), appearance(changed))


def test_cleanup_cancels_without_cache_completion(tmp_path: Path) -> None:
    from test_timeline import make_source

    from sculptcut.domain import Project
    from sculptcut.planning import build_plan
    from sculptcut.timelapse_cleanup import cleanup_ranges

    # Cancellation is checked before any read/detection in a selected interval.
    project = Project(sources=[make_source(0, 0, 10_000_000)])
    for folder in ("proxy", "temp", "logs"):
        (tmp_path / folder).mkdir()
    cancelled = Event()
    cancelled.set()
    with pytest.raises(Cancelled):
        cleanup_ranges(tmp_path, project, build_plan(project, [], 3), cancelled)


def test_real_proxy_cleanup_resumes_completed_windows(tmp_path: Path) -> None:
    from sculptcut.media import discover, run_process
    from sculptcut.planning import RenderDecision, simple_speed_plan
    from sculptcut.project import create_project, database, fingerprint
    from sculptcut.proxy import make_proxies
    from sculptcut.timelapse_cleanup import cleanup_ranges

    source = tmp_path / "input.mp4"
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 "testsrc2=s=128x96:r=10:d=3", "-c:v", "libx264", str(source)])
    before = fingerprint(source)
    root = tmp_path / "project"
    project = create_project(root, [source])
    make_proxies(root, project)
    plan = simple_speed_plan(project, 2)
    sid = project.sources[0].id
    plan = plan.model_copy(update={"decisions": [
        RenderDecision(source_id=sid, source_start_us=0, source_end_us=1_000_000,
                       action="SPEED", output_frame_count=30, provenance="test:work"),
        RenderDecision(source_id=sid, source_start_us=1_000_000, source_end_us=2_000_000,
                       action="DROP", output_frame_count=0, provenance="test:omission"),
        RenderDecision(source_id=sid, source_start_us=2_000_000, source_end_us=3_000_000,
                       action="SPEED", output_frame_count=30, provenance="test:work"),
    ]})
    cancelled = Event()

    def progress(value: float) -> None:
        if value >= 0.5:
            cancelled.set()

    with pytest.raises(Cancelled):
        cleanup_ranges(root, project, plan, cancelled, progress)
    connection = database(root)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM cache WHERE stage='timelapse_window'",
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM cache WHERE stage='timelapse_cleanup'",
        ).fetchone()[0] == 0
    finally:
        connection.close()
    ranges = cleanup_ranges(root, project, plan, None)
    assert cleanup_ranges(root, project, plan, None) == ranges
    assert fingerprint(source) == before


def test_static_input_is_not_stretched_to_a_long_timelapse(tmp_path: Path) -> None:
    from sculptcut.fixtures import generate_fixture
    from sculptcut.planning import simple_speed_plan
    from sculptcut.project import create_project
    from sculptcut.proxy import make_proxies
    from sculptcut.timelapse_cleanup import clean_timelapse

    source = generate_fixture(tmp_path / "static.mp4", "static", duration=3)
    root = tmp_path / "project"
    project = create_project(root, [source])
    make_proxies(root, project)
    plan = simple_speed_plan(project, 30)
    result = clean_timelapse(root, project, plan)
    assert result.actual_frame_count == round(
        project.settings.timelapse_outcome_hold_seconds * result.output_fps,
    )
    assert result.actual_frame_count < result.target_frame_count
    assert any("No visible work" in warning for warning in result.warnings)
    short = clean_timelapse(root, project, simple_speed_plan(project, 1))
    assert short.actual_frame_count == short.output_fps
