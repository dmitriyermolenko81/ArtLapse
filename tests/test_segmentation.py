import pytest

from sculptcut.activity import Activity, Kind
from sculptcut.segmentation import segment_activity


def test_segmentation_coverage_and_spike() -> None:
    labels: list[Kind] = ["STATIC"] * 5
    labels.append("SCULPT")
    labels.extend(["STATIC"] * 5)
    labels.extend(["CAMERA"] * 4)
    rows = [
        Activity(
            source_id="a",
            timestamp_us=i * 200000,
            kind=kind,
            score=0.9,
            components={},
            explanation="test",
        )
        for i, kind in enumerate(labels)
    ]
    result = segment_activity(rows, "a", 3_200_000, 200000)
    assert result[0].start_us == 0 and result[-1].end_us == 3_200_000
    assert all(a.end_us == b.start_us for a, b in zip(result, result[1:], strict=False))
    assert result[0].kind == "STATIC" and result[0].end_us == 2_200_000
    assert result[-1].kind == "UNKNOWN"
    assert segment_activity([], "b", 1_000_000, 200000)[0].kind == "UNKNOWN"


def test_short_mixed_event_retained() -> None:
    rows = [
        Activity(
            source_id="a",
            timestamp_us=0,
            kind="SCULPT",
            score=0.6,
            components={},
            explanation="test",
        )
    ]
    result = segment_activity(rows, "a", 400000, 200000)
    assert result[0].kind == "MIXED"
    assert sum(s.end_us - s.start_us for s in result) == 400000


def test_components_use_duration_including_partial_last_frame() -> None:
    rows = [
        Activity(
            source_id="a", timestamp_us=i * 1_000_000, kind="SCULPT", score=score,
            components={"tracking_quality": quality, "camera_score": camera, "roi_change": change},
            explanation="varying evidence",
        )
        for i, (score, quality, camera, change) in enumerate([
            (0.6, 0.9, 0.1, 0.01), (0.8, 0.7, 0.3, 0.03), (1.0, 0.5, 0.5, 0.05),
        ])
    ]
    original = [row.model_copy(deep=True) for row in rows]
    result = segment_activity(rows, "a", 2_500_000, 1_000_000)
    assert len(result) == 1
    assert result[0].score == pytest.approx(0.76)
    assert result[0].components == pytest.approx({
        "tracking_quality": 0.74, "camera_score": 0.26, "roi_change": 0.026,
        "minimum_tracking_quality": 0.5, "maximum_camera_score": 0.5,
    })
    assert rows == original


def test_missing_measurements_are_not_inherited_from_first_frame() -> None:
    rows = [
        Activity(
            source_id="a", timestamp_us=i * 1_000_000, kind="SCULPT", score=0.6,
            components=components, explanation="partially missing evidence",
        )
        for i, components in enumerate([
            {"tracking_quality": 0.9, "camera_score": 0.1}, {},
            {"tracking_quality": 0.9, "camera_score": 0.1},
        ])
    ]
    assert segment_activity(rows, "a", 3_000_000, 1_000_000)[0].components == {}
