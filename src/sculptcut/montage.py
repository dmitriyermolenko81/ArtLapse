"""Bounded stable-state sampling with safe speed fallback."""

from collections.abc import Iterable
from pathlib import Path

from sculptcut.analysis import FrameFeature, read_features
from sculptcut.domain import Project
from sculptcut.planning import RenderDecision, RenderPlan


def stable_states(
    features: Iterable[FrameFeature], start: int, end: int, limit: int = 12
) -> list[int]:
    if limit < 2:
        return []
    buckets: dict[int, FrameFeature] = {}
    for feature in features:
        if not start <= feature.timestamp_us < end:
            continue
        if (
            feature.measurement_quality < 0.9
            or feature.contrast < 0.03
            or feature.roi_change > 0.002
            or feature.ui_change > 0.002
        ):
            continue
        bucket = min(limit - 1, (feature.timestamp_us - start) * limit // (end - start))
        prior = buckets.get(bucket)
        if prior is None or feature.contrast > prior.contrast:
            buckets[bucket] = feature
    ordered = [buckets[i] for i in sorted(buckets)]
    if len(ordered) < 2:
        return []
    # Only use montage if the first/last states are actually represented near both ends.
    tolerance = max(500000, (end - start) // limit)
    if ordered[0].timestamp_us - start > tolerance or end - ordered[-1].timestamp_us > tolerance:
        return []
    chosen = [ordered[0]]
    for candidate in ordered[1:-1]:
        distance = (
            int(candidate.perceptual_hash, 16) ^ int(chosen[-1].perceptual_hash, 16)
        ).bit_count()
        if distance >= 6:
            chosen.append(candidate)
    chosen.append(ordered[-1])
    return [f.timestamp_us for f in chosen]


def apply_montage(root: Path, project: Project, plan: RenderPlan) -> RenderPlan:
    sources = {s.id: s for s in project.sources}
    decisions: list[RenderDecision] = []
    warnings = plan.warnings[:]
    for decision in plan.decisions:
        speed = (
            (decision.source_end_us - decision.source_start_us)
            * plan.output_fps
            / 1e6
            / max(1, decision.output_frame_count)
        )
        forced = decision.provenance == "user:FORCE_MONTAGE"
        if decision.action == "SPEED" and (
            forced or (speed > 64 and not decision.provenance.startswith("pacing:"))
        ):
            states = stable_states(
                read_features(
                    root,
                    project,
                    sources[decision.source_id],
                    decision.source_start_us,
                    decision.source_end_us,
                ),
                decision.source_start_us,
                decision.source_end_us,
                min(12, decision.output_frame_count),
            )
            if states:
                decision = decision.model_copy(
                    update={"action": "MONTAGE", "sample_times_us": states}
                )
            else:
                warnings.append(
                    f"MONTAGE fallback to SPEED: {decision.source_id}@{decision.source_start_us}"
                )
        decisions.append(decision)
    return plan.model_copy(update={"decisions": decisions, "warnings": warnings})
