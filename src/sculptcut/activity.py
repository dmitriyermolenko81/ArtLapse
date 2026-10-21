"""Explainable rule-based activity scores, never calibrated probabilities."""

from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Literal

from sculptcut.analysis import FrameFeature, analysis_key, read_features
from sculptcut.domain import Model, Project
from sculptcut.idle import DEFAULT_IDLE, quiet
from sculptcut.media import Cancelled
from sculptcut.motion import Motion, analyze_motion
from sculptcut.project import cache_key, database

Kind = Literal[
    "STATIC", "CAMERA", "SCULPT", "INTERFACE", "MIXED", "PROCESSING", "FINAL_VIEW", "UNKNOWN"
]


@dataclass(frozen=True)
class ActivitySettings:
    maximum_local_fraction: float = 0.16
    maximum_camera_residual: float = 0.012
    stable_local_change: float = 0.0007
    stable_edge_change: float = 0.0022
    stable_histogram_change: float = 0.01
    stable_signed_change: float = 0.001
    stable_hash_step_bits: int = 2
    stable_hash_run_bits: int = 8


SETTINGS = ActivitySettings()


class Activity(Model):
    source_id: str
    timestamp_us: int
    kind: Kind
    score: float
    components: dict[str, float]
    explanation: str


def classify(
    feature: FrameFeature,
    motion: Motion,
    final_window: bool = False,
    perceptually_static: bool = False,
) -> Activity:
    components = {
        "roi_change": feature.roi_change,
        "ui_change": feature.ui_change,
        "camera_score": motion.camera_score,
        "tracking_quality": motion.quality,
        "residual": motion.residual,
        "histogram_change": feature.histogram_change,
        "changed_pixel_ratio": feature.changed_pixel_ratio,
    }
    kind: Kind = "UNKNOWN"
    score = 0.2
    explanation = "Insufficient evidence; preserve footage"
    if feature.measurement_quality < 0.9:
        pass
    elif feature.histogram_change > 0.4:
        kind, score, explanation = "UNKNOWN", 0.8, "Scene/app/material switch; preserve"
    elif feature.ui_change > 0.02 and feature.roi_change > 0.02:
        kind, score, explanation = "MIXED", 0.7, "Canvas and interface both changed"
    elif feature.ui_change > max(0.01, feature.roi_change * 3):
        kind, score, explanation = "INTERFACE", 0.7, "Change predominantly outside canvas"
    elif (
        final_window
        and quiet(feature, DEFAULT_IDLE, False)
        and feature.contrast > 0.15
        and motion.quality > 0.5
        and feature.ui_change < 0.001
    ):
        kind, score, explanation = (
            "FINAL_VIEW",
            0.4,
            "Stable detailed end-view hypothesis; review required",
        )
    elif feature.roi_change < 0.00002 and 0.001 < feature.ui_change < 0.01:
        kind, score, explanation = (
            "PROCESSING",
            0.3,
            "Small UI activity hypothesis; not a ZBrush command",
        )
    elif quiet(feature, DEFAULT_IDLE, False):
        kind, score, explanation = "STATIC", 0.9, "Low observed canvas change"
    elif perceptually_static:
        kind, score, explanation = (
            "STATIC",
            0.8,
            "Stable view; local pixel changes did not accumulate",
        )
    elif motion.quality >= 0.5 and motion.camera_score > 0.6:
        if motion.residual <= SETTINGS.maximum_camera_residual:
            kind, score, explanation = (
                "CAMERA",
                motion.camera_score,
                "Coherent 2D motion with low residual",
            )
        else:
            kind, score, explanation = (
                "MIXED",
                0.7,
                "Camera motion with remaining visible edits; preserve",
            )
    elif motion.quality >= 0.5 and motion.displacement > 0.5 and motion.residual > 0.08:
        kind, score, explanation = "MIXED", 0.7, "Global motion with unexplained residual"
    elif feature.roi_change > 0.00002 and motion.quality >= 0.5 and motion.displacement < 0.5:
        if feature.changed_pixel_ratio <= SETTINGS.maximum_local_fraction:
            kind, score, explanation = (
                "SCULPT",
                0.6,
                "Local visible change; not proof of geometry edit",
            )
        else:
            kind, score, explanation = (
                "MIXED",
                0.6,
                "Broad change despite stationary tracks; possible model rotation",
            )
    return Activity(
        source_id=feature.source_id,
        timestamp_us=feature.timestamp_us,
        kind=kind,
        score=score,
        components=components,
        explanation=explanation,
    )


def _stable_view(
    feature: FrameFeature,
    previous: FrameFeature | None,
    anchor_hash: int | None,
) -> tuple[bool, int | None]:
    if previous is None:
        return False, None
    current_hash = int(feature.perceptual_hash, 16)
    previous_hash = int(previous.perceptual_hash, 16)
    if not (
        feature.measurement_quality >= 0.9
        and feature.roi_change <= SETTINGS.stable_local_change
        and feature.edge_change <= SETTINGS.stable_edge_change
        and feature.histogram_change <= SETTINGS.stable_histogram_change
        and abs(feature.signed_change) <= SETTINGS.stable_signed_change
        and (current_hash ^ previous_hash).bit_count() <= SETTINGS.stable_hash_step_bits
    ):
        return False, None
    stable_anchor = anchor_hash if anchor_hash is not None else previous_hash
    if (current_hash ^ stable_anchor).bit_count() > SETTINGS.stable_hash_run_bits:
        return False, None
    return True, stable_anchor


def classify_project(root: Path, project: Project, cancel: Event | None = None) -> None:
    analyze_motion(root, project, cancel)
    connection = database(root)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS activity "
            "(source_id TEXT,key TEXT,timestamp INTEGER,value TEXT,"
            "PRIMARY KEY(source_id,key,timestamp))"
        )
        for source in project.sources:
            key = cache_key("activity-3-perceptual-stability", analysis_key(project, source))
            motion_key = cache_key("motion-2", analysis_key(project, source))
            previous_feature: FrameFeature | None = None
            stable_anchor_hash: int | None = None
            for index, feature in enumerate(read_features(root, project, source)):
                if cancel is not None and cancel.is_set():
                    raise Cancelled("Classification cancelled")
                row = connection.execute(
                    "SELECT value FROM motion WHERE source_id=? AND key=? AND timestamp=?",
                    (source.id, motion_key, feature.timestamp_us),
                ).fetchone()
                motion = Motion.model_validate_json(row[0]) if row else Motion()
                perceptually_static, next_anchor_hash = _stable_view(
                    feature, previous_feature, stable_anchor_hash
                )
                if motion.quality >= 0.5 and motion.displacement < 0.5:
                    stable_anchor_hash = next_anchor_hash
                else:
                    stable_anchor_hash = None
                    perceptually_static = False
                activity = classify(
                    feature,
                    motion,
                    feature.timestamp_us >= source.duration_us - 2_000_000,
                    perceptually_static,
                )
                connection.execute(
                    "INSERT OR REPLACE INTO activity VALUES(?,?,?,?)",
                    (source.id, key, feature.timestamp_us, activity.model_dump_json()),
                )
                if index % 100 == 0:
                    connection.commit()
                previous_feature = feature
        connection.commit()
    finally:
        connection.close()
