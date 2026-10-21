"""Observable novelty heuristic with explicit comparability gates."""

import json
from collections.abc import Iterable
from pathlib import Path
from threading import Event

from sculptcut.analysis import FrameFeature, read_features
from sculptcut.domain import Project
from sculptcut.project import atomic_write
from sculptcut.segmentation import Segment, segment_project


def score_segment(segment: Segment, features: Iterable[FrameFeature]) -> Segment:
    count = 0
    changes = edges = novelty = switches = 0.0
    previous_hash: int | None = None
    for feature in features:
        if not segment.start_us <= feature.timestamp_us < segment.end_us:
            continue
        count += 1
        changes += min(1, feature.roi_change / 0.02)
        edges += min(1, feature.edge_change / 0.05)
        switches += feature.histogram_change > 0.2
        current_hash = int(feature.perceptual_hash, 16)
        if previous_hash is not None:
            novelty += (current_hash ^ previous_hash).bit_count() / 63
        previous_hash = current_hash
    denominator = max(1, count)
    comparable = (
        segment.kind == "SCULPT"
        and segment.score >= 0.5
        and segment.components.get(
            "minimum_tracking_quality", segment.components.get("tracking_quality", 0)
        ) >= 0.5
        and segment.components.get(
            "maximum_camera_score", segment.components.get("camera_score", 1)
        ) < 0.2
        and switches == 0
    )
    observed = min(1, (changes / denominator + edges / denominator + novelty / denominator) / 3)
    # Uncertain evidence receives a protection floor, never a low-value/drop inference.
    value = 0.5
    if comparable:
        value = 0.5 + 0.5 * observed
    elif segment.kind in ("CAMERA", "INTERFACE", "STATIC", "PROCESSING"):
        value = 0.3
    components = segment.components | {
        "observed_change": changes / denominator,
        "edge_change": edges / denominator,
        "novelty": novelty / denominator,
        "comparable": float(comparable),
        "scene_switches": switches,
        "importance": value,
    }
    return segment.model_copy(update={"importance": value, "components": components})


def importance_project(root: Path, project: Project, cancel: Event | None = None) -> list[Segment]:
    segments = segment_project(root, project, cancel)
    sources = {s.id: s for s in project.sources}
    result = [
        score_segment(
            segment,
            read_features(
                root, project, sources[segment.source_id], segment.start_us, segment.end_us
            ),
        )
        for segment in segments
    ]
    atomic_write(root / "segments.json", json.dumps([s.model_dump() for s in result], indent=2))
    return result
