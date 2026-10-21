"""Source-bounded segmentation with full coverage and conservative short events."""

import json
from collections.abc import Iterable
from pathlib import Path
from threading import Event

from pydantic import Field

from sculptcut.activity import Activity, Kind, classify_project
from sculptcut.analysis import analysis_key
from sculptcut.domain import Model, Project
from sculptcut.project import atomic_write, cache_key, database

SEGMENT_VERSION = "segmentation-2-weighted-evidence"


def _merge_components(
    previous: dict[str, float], current: dict[str, float], previous_weight: float,
) -> dict[str, float]:
    """Average observed evidence by duration; retain conservative safety bounds.

    Missing measurements stay unknown rather than borrowing a neighbour's value.
    """
    merged = {
        key: previous[key] * previous_weight + current[key] * (1 - previous_weight)
        for key in sorted(previous.keys() & current.keys())
    }
    if "tracking_quality" in merged:
        merged["minimum_tracking_quality"] = min(
            previous.get("minimum_tracking_quality", previous["tracking_quality"]),
            current.get("minimum_tracking_quality", current["tracking_quality"]),
        )
    if "camera_score" in merged:
        merged["maximum_camera_score"] = max(
            previous.get("maximum_camera_score", previous["camera_score"]),
            current.get("maximum_camera_score", current["camera_score"]),
        )
    return merged


class Segment(Model):
    source_id: str
    start_us: int = Field(ge=0)
    end_us: int = Field(gt=0)
    kind: Kind
    score: float = Field(ge=0, le=1)
    importance: float = Field(default=0.5, ge=0, le=1)
    components: dict[str, float] = Field(default_factory=dict)


def segment_activity(
    rows: Iterable[Activity],
    source_id: str,
    duration_us: int,
    step_us: int,
    minimum_us: int = 600000,
) -> list[Segment]:
    runs: list[Segment] = []
    position = 0
    for row in rows:
        if row.source_id != source_id or row.timestamp_us < position:
            raise ValueError("Activity source/order mismatch")
        if row.timestamp_us >= duration_us:
            break
        if row.timestamp_us > position:
            runs.append(
                Segment(
                    source_id=source_id,
                    start_us=position,
                    end_us=row.timestamp_us,
                    kind="UNKNOWN",
                    score=0,
                )
            )
        end = min(duration_us, row.timestamp_us + step_us)
        if runs and runs[-1].kind == row.kind and runs[-1].end_us == row.timestamp_us:
            previous = runs[-1]
            weight = (previous.end_us - previous.start_us) / (end - previous.start_us)
            runs[-1] = previous.model_copy(
                update={
                    "end_us": end,
                    "score": previous.score * weight + row.score * (1 - weight),
                    "components": _merge_components(previous.components, row.components, weight),
                }
            )
        else:
            runs.append(
                Segment(
                    source_id=source_id,
                    start_us=row.timestamp_us,
                    end_us=end,
                    kind=row.kind,
                    score=row.score,
                    components=row.components,
                )
            )
        position = end
    if position < duration_us:
        runs.append(
            Segment(
                source_id=source_id, start_us=position, end_us=duration_us, kind="UNKNOWN", score=0
            )
        )
    smoothed: list[Segment] = []
    for index, run in enumerate(runs):
        if run.end_us - run.start_us < minimum_us and run.kind != "UNKNOWN":
            if (
                0 < index < len(runs) - 1
                and runs[index - 1].kind == runs[index + 1].kind
                and run.end_us - run.start_us <= step_us
            ):
                run = run.model_copy(
                    update={"kind": runs[index - 1].kind, "score": min(run.score, 0.5)}
                )
            else:
                run = run.model_copy(update={"kind": "MIXED", "score": min(run.score, 0.5)})
        if smoothed and smoothed[-1].kind == run.kind:
            previous = smoothed[-1]
            weight = (previous.end_us - previous.start_us) / (run.end_us - previous.start_us)
            smoothed[-1] = previous.model_copy(
                update={
                    "end_us": run.end_us,
                    "score": min(previous.score, run.score),
                    "components": _merge_components(previous.components, run.components, weight),
                }
            )
        else:
            smoothed.append(run)
    return smoothed


def segment_project(root: Path, project: Project, cancel: Event | None = None) -> list[Segment]:
    classify_project(root, project, cancel)
    connection = database(root)
    result = []
    try:
        for source in project.sources:
            key = cache_key("activity-3-perceptual-stability", analysis_key(project, source))
            rows = connection.execute(
                "SELECT value FROM activity WHERE source_id=? AND key=? ORDER BY timestamp",
                (source.id, key),
            )
            result.extend(
                segment_activity(
                    (Activity.model_validate_json(row[0]) for row in rows),
                    source.id,
                    source.duration_us,
                    round(1_000_000 / project.settings.proxy_fps),
                )
            )
    finally:
        connection.close()
    atomic_write(root / "segments.json", json.dumps([s.model_dump() for s in result], indent=2))
    return result
