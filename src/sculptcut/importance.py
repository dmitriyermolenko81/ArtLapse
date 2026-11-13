"""Observable novelty heuristic with explicit comparability gates."""

import json
from collections.abc import Callable, Iterable
from pathlib import Path
from threading import Event

from sculptcut.activity import activity_ready
from sculptcut.analysis import FrameFeature, analysis_key, read_features
from sculptcut.domain import Project
from sculptcut.media import Cancelled
from sculptcut.project import atomic_write, cache_get, cache_key, cache_put
from sculptcut.segmentation import Segment, segment_key, segment_project

IMPORTANCE_VERSION = 'importance-320'


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


def importance_project(root: Path, project: Project, cancel: Event | None = None,
                       stage: Callable[[str], None] | None = None) -> list[Segment]:
    if cancel and cancel.is_set():
        raise Cancelled('Importance analysis cancelled')
    cached: dict[str, list[Segment]] = {}
    for source in project.sources:
        if cache_get(root, 'features', source.id, analysis_key(project, source)) is not None:
            value = cache_get(root, 'importance', source.id,
                              cache_key(IMPORTANCE_VERSION, segment_key(project, source.id)))
            if value is not None:
                cached[source.id] = [Segment.model_validate(row) for row in value]
    if stage:
        if len(cached) == len(project.sources):
            stage('Используем сохранённый анализ видео')
        elif all(activity_ready(root, project, s) for s in project.sources if s.id not in cached):
            stage('Обновляем оценки из сохранённых измерений')
        else:
            stage('Анализируем активность видео')
    segments = segment_project(root, project, cancel) if len(cached) < len(project.sources) else []
    sources = {s.id: s for s in project.sources}
    measured = []
    for segment in segments:
        if cancel and cancel.is_set():
            raise Cancelled('Importance analysis cancelled')
        if segment.source_id in cached:
            continue
        measured.append(score_segment(
            segment,
            read_features(
                root, project, sources[segment.source_id], segment.start_us, segment.end_us
            ),
        ))
    if cancel and cancel.is_set():
        raise Cancelled('Importance analysis cancelled')
    result = []
    for source in project.sources:
        rows = cached.get(source.id)
        if rows is None:
            rows = [s for s in measured if s.source_id == source.id]
            if cache_get(root, 'features', source.id, analysis_key(project, source)) is not None:
                cache_put(root, 'importance', source.id,
                          cache_key(IMPORTANCE_VERSION, segment_key(project, source.id)),
                          [s.model_dump() for s in rows])
        result.extend(rows)
    atomic_write(root / "segments.json", json.dumps([s.model_dump() for s in result], indent=2))
    return result
