import json
from pathlib import Path

import numpy as np
import pytest

from sculptcut.activity import Activity, Kind
from sculptcut.analysis import extract_features, measure, read_features
from sculptcut.domain import ProjectSettings, Roi
from sculptcut.fixtures import generate_fixture
from sculptcut.importance import score_segment
from sculptcut.planning import plan_project, validate_plan
from sculptcut.project import create_project
from sculptcut.segmentation import Segment, segment_activity


def test_comparable_change_and_false_progress() -> None:
    frame = np.zeros((64, 64), dtype=np.uint8)
    changed = frame.copy()
    changed[10:20, 10:20] = 100
    feature = measure(changed, frame, Roi(x=0, y=0, width=1, height=1), "a", 200000)
    segment = Segment(
        source_id="a",
        start_us=0,
        end_us=1000000,
        kind="SCULPT",
        score=0.6,
        components={"tracking_quality": 0.9, "camera_score": 0},
    )
    scored = score_segment(segment, [feature])
    assert scored.importance > 0.5
    assert score_segment(segment, [feature]) == scored
    for kind in ("CAMERA", "UNKNOWN", "MIXED"):
        suspect = segment.model_copy(update={"kind": kind})
        assert score_segment(suspect, [feature]).components["comparable"] == 0
    mask = feature.model_copy(update={"histogram_change": 0.9})
    assert score_segment(segment, [mask]).importance == 0.5


@pytest.mark.parametrize("spike_kind,quality,camera", [("CAMERA", 0.9, 0.9), ("MIXED", 0.1, 0.0)])
def test_smoothed_uncertainty_is_not_hidden_by_average_evidence(
    spike_kind: Kind, quality: float, camera: float,
) -> None:
    samples: list[tuple[Kind, float, float]] = [
        ("SCULPT", 0.9, 0.0), ("SCULPT", 0.9, 0.0), ("SCULPT", 0.9, 0.0),
        (spike_kind, quality, camera),
        ("SCULPT", 0.9, 0.1), ("SCULPT", 0.9, 0.1), ("SCULPT", 0.9, 0.1),
    ]
    rows = [
        Activity(
            source_id="a", timestamp_us=i * 200_000, kind=kind, score=0.6,
            components={"tracking_quality": tracking, "camera_score": motion},
            explanation="brief navigation between work",
        )
        for i, (kind, tracking, motion) in enumerate(samples)
    ]
    result = segment_activity(rows, "a", 1_400_000, 200_000)
    assert len(result) == 1 and result[0].kind == "SCULPT"
    assert result[0].components["camera_score"] == pytest.approx((camera + 0.3) / 7)
    assert result[0].components["tracking_quality"] == pytest.approx((quality + 5.4) / 7)
    assert result[0].components["maximum_camera_score"] == max(camera, 0.1)
    assert result[0].components["minimum_tracking_quality"] == quality
    assert result[0].score == 0.5
    frame = np.zeros((64, 64), dtype=np.uint8)
    changed = frame.copy()
    changed[10:20, 10:20] = 100
    feature = measure(changed, frame, Roi(x=0, y=0, width=1, height=1), "a", 200_000)
    scored = score_segment(result[0], [feature])
    assert scored.importance == 0.5
    assert scored.components["comparable"] == 0


def test_video_plan_refreshes_segment_evidence_without_reextracting_features(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sculptcut.planning as planning

    source = generate_fixture(tmp_path / "local.mp4", "local", duration=3)
    root = tmp_path / "project"
    project = create_project(root, [source]).model_copy(update={
        "settings": ProjectSettings(
            proxy_width=192, proxy_fps=5, expressive_pacing=False, final_view_seconds=0,
        ),
    })
    extract_features(root, project)
    features = list(read_features(root, project, project.sources[0]))
    current_version = planning.SEGMENT_VERSION
    monkeypatch.setattr(planning, "SEGMENT_VERSION", "legacy-first-frame-evidence")
    old = plan_project(root, project, 1)
    monkeypatch.setattr(planning, "SEGMENT_VERSION", current_version)
    refreshed = plan_project(root, project, 1)
    validate_plan(project, refreshed)
    assert refreshed.settings["request_key"] != old.settings["request_key"]
    assert refreshed.actual_frame_count == 30
    assert list(read_features(root, project, project.sources[0])) == features
    segments = [Segment.model_validate(row) for row in json.loads(
        (root / "segments.json").read_text(encoding="utf-8")
    )]
    assert any("minimum_tracking_quality" in segment.components for segment in segments)
    assert plan_project(root, project, 1) == refreshed
    monkeypatch.setattr(planning, "development_key", lambda: "next-development-version")
    changed_development = plan_project(root, project, 1)
    validate_plan(project, changed_development)
    assert changed_development.settings["request_key"] != refreshed.settings["request_key"]
    assert list(read_features(root, project, project.sources[0])) == features
