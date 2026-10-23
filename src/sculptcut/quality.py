"""Duration-weighted evaluation; unreviewed annotations never yield quality claims."""

import html
import json
from pathlib import Path
from typing import Literal, get_args

from pydantic import Field, model_validator

from sculptcut.activity import Kind
from sculptcut.analysis import extract_features
from sculptcut.domain import Model, ProjectSettings
from sculptcut.planning import RenderDecision, RenderPlan, plan_project
from sculptcut.project import atomic_write, create_project, load_project, validate_sources
from sculptcut.segmentation import Segment


class Label(Model):
    start_us: int = Field(ge=0)
    end_us: int = Field(gt=0)
    kind: Kind


def coverage(labels: list[Label], duration: int) -> None:
    position = 0
    for label in labels:
        if label.start_us != position or label.end_us <= position:
            raise ValueError("Labels must cover the source without gaps or overlaps")
        position = label.end_us
    if position != duration:
        raise ValueError("Label duration does not match source")


class AnnotatedClip(Model):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    path: Path
    duration_us: int = Field(gt=0)
    annotation_origin: Literal["human", "synthetic", "draft"]
    reviewed: bool
    segments: list[Label]

    @model_validator(mode="after")
    def validate_coverage(self) -> "AnnotatedClip":
        coverage(self.segments, self.duration_us)
        if self.annotation_origin == "draft" and self.reviewed:
            raise ValueError("Draft annotations cannot be marked reviewed")
        return self


class Annotations(Model):
    schema_version: Literal[1] = 1
    clips: list[AnnotatedClip]
    multi_source_order: list[str] = Field(default_factory=list)
    note: str = ""

    @model_validator(mode="after")
    def unique_ids(self) -> "Annotations":
        ids = {clip.id for clip in self.clips}
        if len(ids) != len(self.clips) or not set(self.multi_source_order).issubset(ids):
            raise ValueError("Duplicate or unknown clip IDs")
        return self


def sculpt_interval_coverage(labels: list[Label], decisions: list[RenderDecision]) -> int:
    """Measure the union of source intervals used against labeled sculpting time."""
    intervals = sorted((d.source_start_us, d.source_end_us) for d in decisions)
    merged: list[tuple[int, int]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    sculpt = [label for label in labels if label.kind == "SCULPT"]
    total = 0
    i = j = 0
    while i < len(sculpt) and j < len(merged):
        label = sculpt[i]
        start, end = merged[j]
        total += max(0, min(label.end_us, end) - max(label.start_us, start))
        if label.end_us <= end:
            i += 1
        else:
            j += 1
    return total


def evaluate(
    clip: AnnotatedClip,
    predictions: list[Label],
    plan: RenderPlan,
    source_id: str,
    override_count: int = 0,
) -> dict[str, object]:
    if not clip.reviewed:
        return {"status": "NOT_VERIFIED", "metrics": None, "origin": clip.annotation_origin}
    coverage(predictions, clip.duration_us)
    kinds = get_args(Kind)
    confusion = {actual: {predicted: 0 for predicted in kinds} for actual in kinds}
    i = j = 0
    while i < len(clip.segments) and j < len(predictions):
        truth, predicted = clip.segments[i], predictions[j]
        overlap = max(
            0, min(truth.end_us, predicted.end_us) - max(truth.start_us, predicted.start_us)
        )
        confusion[truth.kind][predicted.kind] += overlap
        if truth.end_us <= predicted.end_us:
            i += 1
        if predicted.end_us <= truth.end_us:
            j += 1
    per_class = {}
    for kind in kinds:
        actual_us = sum(confusion[kind].values())
        predicted_us = sum(row[kind] for row in confusion.values())
        true_positive = confusion[kind][kind]
        per_class[kind] = {
            "precision": true_positive / predicted_us if predicted_us else None,
            "recall": true_positive / actual_us if actual_us else None,
            "support_us": actual_us,
        }
    sculpt_us = sum(s.end_us - s.start_us for s in clip.segments if s.kind == "SCULPT")
    deleted = sculpt_interval_coverage(
        clip.segments,
        [d for d in plan.decisions if d.source_id == source_id and d.action == "DROP"],
    )
    covered = sculpt_interval_coverage(
        clip.segments,
        [d for d in plan.decisions if d.source_id == source_id and d.action in ("KEEP", "SPEED")],
    )
    return {
        "status": "SYNTHETIC_ONLY" if clip.annotation_origin == "synthetic" else "REVIEWED",
        "origin": clip.annotation_origin,
        "metrics": {
            "confusion_us": confusion,
            "per_class": per_class,
            "sculpt_deleted_us": deleted,
            "sculpt_deleted_fraction": deleted / sculpt_us if sculpt_us else None,
            "sculpt_source_covered_us": covered,
            "sculpt_source_covered_fraction": covered / sculpt_us if sculpt_us else None,
            "sculpt_source_omitted_us": sculpt_us - covered,
            "sculpt_source_omitted_fraction": (sculpt_us - covered) / sculpt_us
            if sculpt_us
            else None,
            "unknown_fraction": sum(row["UNKNOWN"] for row in confusion.values())
            / clip.duration_us,
            "duration_error_seconds": (plan.actual_frame_count - plan.target_frame_count)
            / plan.output_fps,
            "override_count": override_count,
            "overrides_per_source_minute": override_count * 60_000_000 / clip.duration_us,
        },
    }


def evaluate_suite(
    annotations: Path,
    output: Path,
    target_seconds: float = 1.5,
    preset: Literal["timelapse", "shorts"] = "timelapse",
) -> Path:
    document = Annotations.model_validate_json(annotations.read_text(encoding="utf-8"))
    output.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, object]] = []
    for clip in document.clips:
        if not clip.reviewed:
            results.append(
                {
                    "clip": clip.id,
                    "status": "NOT_VERIFIED",
                    "origin": clip.annotation_origin,
                    "metrics": None,
                }
            )
            continue
        source_path = (annotations.parent / clip.path).resolve()
        root = output / "projects" / clip.id
        project = (
            load_project(root)
            if (root / "project.json").exists()
            else create_project(root, [source_path])
        )
        if len(project.sources) != 1 or project.sources[0].path.resolve() != source_path:
            raise ValueError("Evaluation project belongs to another source")
        source = project.sources[0]
        if source.duration_us != clip.duration_us:
            raise ValueError("Annotation duration differs from probed source")
        validate_sources(root, project)
        extract_features(root, project)
        for style in ("balanced",) if preset == "shorts" else ("calm", "balanced", "dynamic"):
            settings = ProjectSettings.model_validate(
                project.settings.model_dump()
                | {
                    "style": style,
                    "preset": preset,
                    "target_seconds": 60 if preset == "shorts" else target_seconds,
                }
            )
            variant = project.model_copy(update={"settings": settings})
            plan = plan_project(root, variant, 60 if preset == "shorts" else target_seconds)
            segments = [
                Segment.model_validate(row)
                for row in json.loads((root / "segments.json").read_text(encoding="utf-8"))
            ]
            labels = [Label(start_us=s.start_us, end_us=s.end_us, kind=s.kind) for s in segments]
            result = evaluate(clip, labels, plan, source.id, len(project.overrides))
            results.append(
                {
                    "clip": clip.id,
                    "preset": preset,
                    "style": style,
                    "source_fingerprint": source.fingerprint,
                    **result,
                }
            )
            atomic_write(root / f"plan-{preset}-{style}.json", plan.model_dump_json(indent=2))
    report = {
        "schema_version": 1,
        "method": (
            "Duration-weighted microsecond intersections; "
            "the requested preset changes planning, not classification."
        ),
        "limitations": (
            "Synthetic results do not validate real sculpting. "
            "Override count is observed, not required editing effort. "
            "Duration error measures plan, not encoded file. "
            "Sculpt source coverage counts KEEP/SPEED intervals, not every encoded frame; "
            "MONTAGE samples do not count as continuous coverage. "
            "Episodic Shorts intentionally omit source intervals; omission is descriptive."
        ),
        "evaluated_preset": preset,
        "results": results,
    }
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    atomic_write(output / "quality.json", serialized)
    atomic_write(
        output / "quality.html",
        '<!doctype html><meta charset="utf-8"><title>ArtLapse quality</title>'
        "<h1>Оценка качества</h1><pre>" + html.escape(serialized) + "</pre>",
    )
    return output / "quality.html"
