"""Explicit beginning/middle/end pacing; uncertain actions are never discarded."""

import math
from dataclasses import dataclass

from sculptcut.budget import distribute
from sculptcut.domain import Project
from sculptcut.planning import RenderDecision, RenderPlan, validate_plan
from sculptcut.segmentation import Segment


@dataclass(frozen=True)
class ActivityPacingSettings:
    minimum_run_us: int = 1_200_000
    minimum_confidence: float = 0.6
    sculpt_weight: float = 1.5
    navigation_weight: float = 0.15


ACTIVITY_PACING = ActivityPacingSettings()


def sustained(segment: Segment) -> bool:
    return (
        segment.score >= ACTIVITY_PACING.minimum_confidence
        and segment.end_us - segment.start_us >= ACTIVITY_PACING.minimum_run_us
        and segment.kind in ("SCULPT", "CAMERA", "INTERFACE", "STATIC", "PROCESSING")
    )


def paced_plan(project: Project, base: RenderPlan, segments: list[Segment]) -> RenderPlan:
    settings, fps = project.settings, base.output_fps
    window = round(settings.pacing_window_seconds * 1e6)
    anchor = min(round(settings.pacing_anchor_seconds * 1e6), window // 4)
    last = project.sources[-1]
    # Reserve at most one fifth of the requested movie; short fixtures stay useful.
    final_frames = min(
        round(settings.final_view_seconds * fps),
        base.target_frame_count // 5,
        last.duration_us * fps // 1_000_000,
    )
    tail = last.duration_us - (final_frames * 1_000_000 + fps - 1) // fps
    pieces: list[RenderDecision] = []
    minimum: list[int] = []
    maximum: list[int] = []
    weights: list[float] = []
    for decision in base.decisions:
        start, end = decision.source_start_us, decision.source_end_us
        if decision.source_id == last.id:
            end = min(end, tail)
        if start >= end:
            continue
        relevant = [
            s
            for s in segments
            if s.source_id == decision.source_id and s.start_us < end and s.end_us > start
        ]
        boundaries = {start, end}
        for segment in relevant:
            if sustained(segment):
                boundaries.update(p for p in (segment.start_us, segment.end_us) if start < p < end)
        for block in range(start // window, (end - 1) // window + 1):
            for offset in (0, anchor, window - anchor, window):
                point = block * window + offset
                if start < point < end:
                    boundaries.add(point)
        ordered = sorted(boundaries)
        for left, right in zip(ordered, ordered[1:], strict=False):
            length = right - left
            cap = max(1, math.floor(length * fps / 1e6))
            if decision.action == "DROP":
                action, low, high, weight, reason = "DROP", 0, 0, 1.0, decision.provenance
            else:
                unknown = change = novelty = unsafe = covered = 0.0
                sculpt = navigation = 0.0
                for segment in relevant:
                    overlap = max(0, min(right, segment.end_us) - max(left, segment.start_us))
                    if not overlap:
                        continue
                    share = overlap / length
                    covered += share
                    unknown += share * (segment.kind in ("UNKNOWN", "MIXED"))
                    if sustained(segment):
                        sculpt += share * (segment.kind == "SCULPT")
                        navigation += share * (segment.kind != "SCULPT")
                    change += share * segment.components.get("observed_change", 0.5)
                    novelty += share * segment.components.get("novelty", 0.5)
                    unsafe += share * (segment.components.get("scene_switches", 0) > 0)
                unknown += max(0.0, 1.0 - covered)
                is_anchor = left % window < anchor or left % window >= window - anchor
                repetitive = unknown < 0.1 and unsafe == 0 and change < settings.pacing_low_change
                # Relative allocation makes contrasts visible without claiming geometry progress.
                emphasis = 0.3 + min(1, change) * 3 + min(1, novelty) * 2
                anchor_boost = {"calm": 2.0, "balanced": 4.0, "dynamic": 6.0}[settings.style]
                emphasis *= anchor_boost if is_anchor else (0.2 if repetitive else 1)
                emphasis *= (
                    1
                    + sculpt * (ACTIVITY_PACING.sculpt_weight - 1)
                    - navigation * (1 - ACTIVITY_PACING.navigation_weight)
                )
                speed_limit = 32 if unknown >= 0.1 else settings.pacing_max_speed
                action, low, high = "SPEED", max(1, math.ceil(cap / speed_limit)), cap
                weight = max(1, cap * emphasis)
                reason = "pacing:" + (
                    "anchor" if is_anchor else "repetitive_middle" if repetitive else "activity"
                )
                if unknown >= 0.1:
                    reason += ":retain_uncertain"
            pieces.append(
                decision.model_copy(
                    update={
                        "source_start_us": left,
                        "source_end_us": right,
                        "action": action,
                        "output_frame_count": low,
                        "provenance": reason,
                    }
                )
            )
            minimum.append(low)
            maximum.append(high)
            weights.append(weight)
    if final_frames:
        pieces.append(
            RenderDecision(
                source_id=last.id,
                source_start_us=tail,
                source_end_us=last.duration_us,
                action="KEEP",
                output_frame_count=final_frames,
                provenance="user:final_tail",
            )
        )
        minimum.append(final_frames)
        maximum.append(final_frames)
        weights.append(float(final_frames))
    actual = max(sum(minimum), min(sum(maximum), base.target_frame_count))
    counts = distribute(minimum, weights, maximum, actual)
    result = base.model_copy(
        update={
            "decisions": [
                d.model_copy(update={"output_frame_count": n})
                for d, n in zip(pieces, counts, strict=True)
            ],
            "actual_frame_count": actual,
            "settings": base.settings
            | {"pacing": "begin-middle-end-1", "final_view_frames": final_frames},
            "warnings": []
            if actual == base.target_frame_count
            else ["Pacing constraints make target infeasible"],
        }
    )
    validate_plan(project, result)
    return result
