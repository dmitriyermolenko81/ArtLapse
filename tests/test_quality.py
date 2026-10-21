import json
from pathlib import Path

import pytest

from sculptcut.fixtures import generate_fixture
from sculptcut.media import probe
from sculptcut.planning import RenderDecision, RenderPlan
from sculptcut.quality import AnnotatedClip, Label, evaluate, evaluate_suite


def test_exact_intersections_and_no_fabricated_metrics() -> None:
    clip = AnnotatedClip(
        id="example",
        path=Path("example.mp4"),
        duration_us=10_000_000,
        annotation_origin="human",
        reviewed=True,
        segments=[
            Label(start_us=0, end_us=6_000_000, kind="SCULPT"),
            Label(start_us=6_000_000, end_us=10_000_000, kind="STATIC"),
        ],
    )
    predictions = [
        Label(start_us=0, end_us=4_000_000, kind="SCULPT"),
        Label(start_us=4_000_000, end_us=10_000_000, kind="UNKNOWN"),
    ]
    plan = RenderPlan(
        source_fingerprints={},
        settings={},
        output_fps=30,
        target_frame_count=30,
        actual_frame_count=45,
        decisions=[
            RenderDecision(
                source_id="s",
                source_start_us=5_000_000,
                source_end_us=7_000_000,
                action="DROP",
                output_frame_count=0,
                provenance="test",
            )
        ],
    )
    result = evaluate(clip, predictions, plan, "s", 2)
    metrics = result["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["per_class"]["SCULPT"]["recall"] == pytest.approx(2 / 3)
    assert metrics["per_class"]["SCULPT"]["precision"] == 1
    assert metrics["per_class"]["CAMERA"]["recall"] is None
    assert metrics["sculpt_deleted_us"] == 1_000_000
    assert metrics["sculpt_deleted_fraction"] == pytest.approx(1 / 6)
    assert metrics["sculpt_source_covered_us"] == 0
    assert metrics["sculpt_source_omitted_us"] == 6_000_000
    assert metrics["unknown_fraction"] == 0.6
    assert metrics["duration_error_seconds"] == 0.5
    assert metrics["overrides_per_source_minute"] == 12
    assert evaluate(clip.model_copy(update={"reviewed": False}), [], plan, "s")["metrics"] is None
    with pytest.raises(ValueError, match="gaps"):
        evaluate(clip, [predictions[1]], plan, "s")
    with pytest.raises(ValueError):
        AnnotatedClip.model_validate(clip.model_dump() | {"duration_us": 12_000_000})


def test_implicit_episode_omission_counts_sculpt_not_shown() -> None:
    clip = AnnotatedClip(
        id="episodes",
        path=Path("episode.mp4"),
        duration_us=10_000_000,
        annotation_origin="human",
        reviewed=True,
        segments=[Label(start_us=0, end_us=10_000_000, kind="SCULPT")],
    )
    plan = RenderPlan(
        source_fingerprints={},
        settings={},
        output_fps=30,
        target_frame_count=30,
        actual_frame_count=30,
        decisions=[
            RenderDecision(
                source_id="s",
                source_start_us=0,
                source_end_us=2_000_000,
                action="SPEED",
                output_frame_count=15,
                provenance="episode-1",
            ),
            RenderDecision(
                source_id="s",
                source_start_us=8_000_000,
                source_end_us=10_000_000,
                action="SPEED",
                output_frame_count=15,
                provenance="episode-2",
            ),
        ],
    )
    metrics = evaluate(clip, [clip.segments[0]], plan, "s")["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["sculpt_deleted_us"] == 0
    assert metrics["sculpt_source_covered_us"] == 4_000_000
    assert metrics["sculpt_source_omitted_us"] == 6_000_000
    assert metrics["sculpt_source_omitted_fraction"] == pytest.approx(0.6)


def test_unreviewed_missing_private_video_is_not_evaluated(tmp_path: Path) -> None:
    annotation = tmp_path / "annotations.json"
    annotation.write_text(
        json.dumps(
            {
                "clips": [
                    {
                        "id": "private",
                        "path": "missing.mp4",
                        "duration_us": 1_000_000,
                        "annotation_origin": "human",
                        "reviewed": False,
                        "segments": [{"start_us": 0, "end_us": 1_000_000, "kind": "UNKNOWN"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    report = evaluate_suite(annotation, tmp_path / "report")
    assert "NOT_VERIFIED" in report.read_text(encoding="utf-8")
    payload = json.loads(report.with_suffix(".json").read_text(encoding="utf-8"))
    assert payload["results"][0]["metrics"] is None


def test_agent_draft_cannot_be_counted_as_reviewed() -> None:
    draft = {
        "id": "real-draft",
        "path": "real.mp4",
        "duration_us": 1_000_000,
        "annotation_origin": "draft",
        "reviewed": False,
        "segments": [{"start_us": 0, "end_us": 1_000_000, "kind": "UNKNOWN"}],
    }
    assert AnnotatedClip.model_validate(draft).reviewed is False
    with pytest.raises(ValueError, match="Draft annotations"):
        AnnotatedClip.model_validate(draft | {"reviewed": True})


def test_reviewed_short_is_evaluated_with_its_actual_preset(tmp_path: Path) -> None:
    source = generate_fixture(tmp_path / "local.mp4", "local", duration=6)
    duration = probe(source).duration_us
    assert duration is not None
    annotations = tmp_path / "annotations.json"
    annotations.write_text(
        json.dumps(
            {
                "clips": [
                    {
                        "id": "local",
                        "path": source.name,
                        "duration_us": duration,
                        "annotation_origin": "synthetic",
                        "reviewed": True,
                        "segments": [{"start_us": 0, "end_us": duration, "kind": "SCULPT"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    report_path = evaluate_suite(annotations, tmp_path / "evaluation", preset="shorts")
    report = json.loads(report_path.with_suffix(".json").read_text(encoding="utf-8"))
    assert report["evaluated_preset"] == "shorts"
    assert len(report["results"]) == 1
    result = report["results"][0]
    assert result["status"] == "SYNTHETIC_ONLY"
    assert result["preset"] == "shorts"
    assert result["metrics"]["sculpt_source_omitted_us"] >= 0
