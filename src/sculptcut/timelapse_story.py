"""Fixed-frame, chronological episodes for the ordinary timelapse preset."""

import math
from collections import defaultdict
from dataclasses import dataclass

from sculptcut.budget import distribute
from sculptcut.development import SETTINGS as DEVELOPMENT
from sculptcut.development import (
    DevelopmentEvidence,
    DevelopmentScorer,
    chronological_weights,
    development_weight,
)
from sculptcut.domain import Project
from sculptcut.planning import RenderDecision, RenderPlan, validate_plan
from sculptcut.segmentation import Segment


@dataclass(frozen=True)
class WorkSpan:
    decision: RenderDecision
    cumulative_start_us: int
    cumulative_end_us: int


def _score(
    source_id: str, start: int, end: int, segments: list[Segment], static_min_score: float,
) -> float:
    weights = {
        "SCULPT": 2.0,
        "FINAL_VIEW": 1.5,
        "MIXED": 0.45,
        "UNKNOWN": 0.3,
        "CAMERA": -2.0,
        "INTERFACE": -2.0,
        "PROCESSING": -2.0,
        "STATIC": -1.0,
    }
    return sum(
        max(0, min(end, s.end_us) - max(start, s.start_us))
        * (weights["UNKNOWN"] if s.kind == "STATIC" and s.score < static_min_score
           else weights[s.kind])
        * (1 + s.importance / 2 if s.kind in ("SCULPT", "FINAL_VIEW") else 1)
        for s in segments
        if s.source_id == source_id and s.start_us < end and s.end_us > start
    )


def _cut_through_work(source_id: str, point: int, segments: list[Segment]) -> bool:
    """A visible work run is a poor place for an editorial cut."""
    return any(
        segment.source_id == source_id
        and segment.kind == "SCULPT"
        and segment.score >= 0.6
        and segment.end_us - segment.start_us >= 1_200_000
        and segment.start_us < point < segment.end_us
        for segment in segments
    )


def _select_windows(
    project: Project, base: RenderPlan, segments: list[Segment],
    development: DevelopmentScorer | None = None,
) -> list[tuple[str, int, int]]:
    spans: list[WorkSpan] = []
    position = 0
    for decision in base.decisions:
        if decision.action == "DROP":
            continue
        length = decision.source_end_us - decision.source_start_us
        spans.append(WorkSpan(decision, position, position + length))
        position += length
    if not spans:
        raise ValueError("Timelapse has no retained footage")
    settings = project.settings
    window_us = settings.timelapse_episode_source_seconds * 1_000_000
    context_us = round(settings.timelapse_action_context_seconds * 1_000_000)
    requested = max(
        1,
        round(
            base.target_frame_count / base.output_fps / settings.timelapse_episode_output_seconds
        ),
    )
    count = min(settings.timelapse_episode_limit, requested, max(1, position // window_us))
    selected: list[tuple[str, int, int]] = []
    prior_states: dict[str, DevelopmentEvidence] = {}
    power = (
        settings.timelapse_early_chapter_power
        if position >= settings.timelapse_early_focus_min_seconds * 1_000_000
        else 1.0
    )
    boundaries = [round(position * (index / count) ** power) for index in range(count + 1)]
    for chapter in range(count):
        left, right = boundaries[chapter], boundaries[chapter + 1]
        if right <= left:
            continue
        candidates: list[tuple[tuple[float, int, int, int], str, int, int]] = []
        for span in spans:
            if span.cumulative_start_us >= right or span.cumulative_end_us <= left:
                continue
            a = (
                span.decision.source_start_us
                + max(left, span.cumulative_start_us)
                - span.cumulative_start_us
            )
            b = (
                span.decision.source_start_us
                + min(right, span.cumulative_end_us)
                - span.cumulative_start_us
            )
            length = min(window_us, b - a)
            middle = (a + b - length) // 2
            starts = {a + (b - a - length) * i // 4 for i in range(5)}
            relevant = [
                segment
                for segment in segments
                if segment.source_id == span.decision.source_id
                and segment.end_us > a
                and segment.start_us < b
            ]
            for segment in relevant:
                if (
                    segment.kind != "SCULPT"
                    or segment.score < 0.6
                    or segment.end_us - segment.start_us < 1_200_000
                ):
                    continue
                for anchor in (segment.start_us - context_us, segment.end_us + context_us - length):
                    starts.add(max(a, min(b - length, anchor)))
            for start in starts:
                end = start + length
                boundary_cuts = sum(
                    (
                        start > span.decision.source_start_us
                        and _cut_through_work(span.decision.source_id, start, relevant),
                        end < span.decision.source_end_us
                        and _cut_through_work(span.decision.source_id, end, relevant),
                    )
                )
                rank = (
                    _score(span.decision.source_id, start, end, relevant,
                           settings.timelapse_static_min_score)
                    - boundary_cuts * 2 * context_us,
                    length,
                    -abs(start - middle),
                    -start,
                )
                candidates.append((rank, span.decision.source_id, start, end))
                candidates.sort(key=lambda item: item[0], reverse=True)
                limit = DEVELOPMENT.candidate_limit if development is not None else 1
                del candidates[limit:]
        if not candidates:
            continue
        best: tuple[tuple[float, int, int, int], str, int, int] | None = candidates[0]
        best_evidence = DevelopmentEvidence()
        if development is not None:
            best = None
            for rank, source_id, start, end in candidates:
                if rank[0] < 0:
                    continue
                evidence = development(source_id, start, end)
                weight = development_weight(evidence, prior_states.get(source_id))
                candidate = ((rank[0] * weight, *rank[1:]), source_id, start, end)
                if best is None or candidate[0] > best[0]:
                    best, best_evidence = candidate, evidence
        if best is not None and best[0][0] >= 0:
            selected.append((best[1], best[2], best[3]))
            prior_states[best[1]] = best_evidence
    return selected


def episodic_timelapse_plan(
    project: Project, base: RenderPlan, segments: list[Segment],
    development: DevelopmentScorer | None = None,
) -> RenderPlan:
    """Keep useful chronological windows and remove sustained holds inside them."""
    selected = _select_windows(project, base, segments, development)
    if not selected:
        raise ValueError("No useful timelapse episodes found")
    development_weights = chronological_weights(selected, development)
    windows: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for source_id, start, end in selected:
        windows[source_id].append((start, end))
    static_holds: dict[str, list[tuple[int, int]]] = defaultdict(list)
    outcome_holds: dict[str, list[tuple[int, int]]] = defaultdict(list)
    static_minimum_us = round(project.settings.timelapse_static_cut_seconds * 1_000_000)
    outcome_hold_us = round(project.settings.timelapse_outcome_hold_seconds * 1_000_000)
    prior_work: Segment | None = None
    for segment in sorted(segments, key=lambda item: (item.source_id, item.start_us)):
        if (segment.kind == "STATIC"
                and segment.score >= project.settings.timelapse_static_min_score
                and segment.end_us - segment.start_us >= static_minimum_us):
            follows_work = (
                prior_work is not None
                and prior_work.source_id == segment.source_id
                and prior_work.end_us == segment.start_us
                and prior_work.kind == "SCULPT"
                and prior_work.score >= 0.6
            )
            hold_end = min(segment.end_us, segment.start_us + outcome_hold_us)
            if follows_work and hold_end > segment.start_us:
                outcome_holds[segment.source_id].append((segment.start_us, hold_end))
            static_holds[segment.source_id].append(
                (hold_end if follows_work else segment.start_us, segment.end_us)
            )
        prior_work = segment
    fps = base.output_fps
    decisions: list[RenderDecision] = []
    minimum: list[int] = []
    maximum: list[int] = []
    weights: list[float] = []
    for original in base.decisions:
        cuts = {original.source_start_us, original.source_end_us}
        if original.action != "DROP":
            for start, end in windows[original.source_id]:
                if start < original.source_end_us and end > original.source_start_us:
                    cuts.update(
                        (max(start, original.source_start_us), min(end, original.source_end_us))
                    )
            for start, end in static_holds[original.source_id]:
                if start < original.source_end_us and end > original.source_start_us:
                    cuts.update(
                        (max(start, original.source_start_us), min(end, original.source_end_us))
                    )
            for start, end in outcome_holds[original.source_id]:
                if start < original.source_end_us and end > original.source_start_us:
                    cuts.update(
                        (max(start, original.source_start_us), min(end, original.source_end_us))
                    )
        ordered = sorted(cuts)
        for start, end in zip(ordered, ordered[1:], strict=False):
            window = next((
                (original.source_id, a, b) for a, b in windows[original.source_id]
                if a <= start and end <= b
            ), None)
            chosen = original.action != "DROP" and window is not None
            stable_hold = chosen and any(
                a <= start and end <= b for a, b in static_holds[original.source_id]
            )
            outcome_hold = chosen and any(
                a <= start and end <= b for a, b in outcome_holds[original.source_id]
            )
            keep = chosen and not stable_hold
            capacity = max(1, (end - start) * fps // 1_000_000) if keep else 0
            lower = (
                capacity
                if outcome_hold
                else max(
                    1,
                    (
                        (end - start) * fps
                        + 1_000_000 * project.settings.timelapse_episode_max_speed
                        - 1
                    )
                    // (1_000_000 * project.settings.timelapse_episode_max_speed),
                )
                if keep
                else 0
            )
            decisions.append(
                original.model_copy(
                    update={
                        "source_start_us": start,
                        "source_end_us": end,
                        "action": "KEEP" if outcome_hold else "SPEED" if keep else "DROP",
                        "output_frame_count": lower,
                        "provenance": "story:outcome_hold"
                        if outcome_hold
                        else "story:static_episode"
                        if keep
                        else original.provenance
                        if original.action == "DROP"
                        else "story:stable_hold"
                        if stable_hold
                        else "story:episode_omission",
                        "subject_camera": None,
                    }
                )
            )
            minimum.append(lower)
            maximum.append(capacity)
            weights.append(float(max(1, capacity)) * development_weights.get(window, 1.0)
                           if window is not None else float(max(1, capacity)))
    target = base.target_frame_count
    actual = max(sum(minimum), min(sum(maximum), target))
    counts = distribute(minimum, weights, maximum, actual)
    result = base.model_copy(
        update={
            "decisions": [
                decision.model_copy(update={"output_frame_count": frames})
                for decision, frames in zip(decisions, counts, strict=True)
            ],
            "actual_frame_count": actual,
            "settings": base.settings
            | {"story": "static_episodes", "episode_count": len(selected)},
            "warnings": [
                "Timelapse uses chronological episode cuts; omissions are editorial selections"
            ]
            + (
                ["Episode speed limits make the requested duration infeasible"]
                if actual != target
                else []
            ),
        }
    )
    validate_plan(project, result)
    return result


def reserve_final_view(project: Project, plan: RenderPlan) -> RenderPlan:
    """Append a recorded clean tail at normal speed, within the output budget."""
    fps = plan.output_fps
    desired_frames = min(
        round(project.settings.final_view_seconds * fps),
        round(project.settings.timelapse_static_cut_seconds * fps),
        plan.target_frame_count // 5,
    )
    if desired_frames <= 0:
        return plan
    last = project.sources[-1]
    allowed: list[tuple[int, int]] = []
    for decision in plan.decisions:
        if decision.source_id != last.id:
            continue
        if decision.action == "DROP":
            continue
        if decision.provenance.startswith("user:") and decision.provenance not in (
            "user:KEEP",
            "user:final_tail",
        ):
            continue
        if allowed and allowed[-1][1] == decision.source_start_us:
            allowed[-1] = (allowed[-1][0], decision.source_end_us)
        else:
            allowed.append((decision.source_start_us, decision.source_end_us))
    if not allowed:
        return plan.model_copy(
            update={"warnings": plan.warnings + ["No clean final view available"]}
        )
    interval_start, interval_end = allowed[-1]
    final_frames = min(desired_frames, (interval_end - interval_start) * fps // 1_000_000)
    if final_frames <= 0:
        return plan.model_copy(
            update={"warnings": plan.warnings + ["No decodable final view available"]}
        )
    final_start = interval_end - (final_frames * 1_000_000 + fps - 1) // fps
    decisions: list[RenderDecision] = []
    minimum: list[int] = []
    maximum: list[int] = []
    preferred: list[float] = []
    for decision in plan.decisions:
        cuts = {decision.source_start_us, decision.source_end_us}
        if decision.source_id == last.id:
            for boundary in (final_start, interval_end):
                if decision.source_start_us < boundary < decision.source_end_us:
                    cuts.add(boundary)
        ordered = sorted(cuts)
        for start, end in zip(ordered, ordered[1:], strict=False):
            final = decision.source_id == last.id and final_start <= start < interval_end
            length = end - start
            cap = max(1, length * fps // 1_000_000)
            if final:
                # The final may span multiple prior decisions; exact frame counts
                # are allocated below while all its pieces remain fixed at 1x.
                low = high = cap
                action = "KEEP"
                provenance = "user:final_tail"
                weight = float(cap)
            elif decision.action == "DROP":
                low = high = 0
                action = "DROP"
                provenance = decision.provenance
                weight = 1.0
            else:
                fixed = decision.provenance.startswith("user:") or decision.provenance == (
                    "story:outcome_hold"
                )
                count = max(
                    1,
                    round(
                        decision.output_frame_count
                        * length
                        / (decision.source_end_us - decision.source_start_us)
                    ),
                )
                low = high = (
                    min(cap, count)
                    if fixed
                    else max(1, math.ceil(cap / (
                        project.settings.timelapse_bridge_max_speed
                        if decision.provenance == "story:process_bridge"
                        else project.settings.timelapse_episode_max_speed
                    )))
                )
                if not fixed:
                    high = cap
                    if decision.provenance == "story:process_bridge":
                        from sculptcut.timelapse_coverage import bridge_minimum_frames

                        low = max(low, bridge_minimum_frames(project, cap, fps))
                action = decision.action
                provenance = decision.provenance
                weight = float(max(1, count))
            decisions.append(
                decision.model_copy(
                    update={
                        "source_start_us": start,
                        "source_end_us": end,
                        "action": action,
                        "output_frame_count": low,
                        "provenance": provenance,
                        "subject_camera": None,
                    }
                )
            )
            minimum.append(low)
            maximum.append(high)
            preferred.append(weight)
    merged_decisions: list[RenderDecision] = []
    merged_minimum: list[int] = []
    merged_maximum: list[int] = []
    merged_preferred: list[float] = []
    for decision, low, high, weight in zip(decisions, minimum, maximum, preferred, strict=True):
        if (
            decision.provenance == "user:final_tail"
            and merged_decisions
            and merged_decisions[-1].provenance == "user:final_tail"
            and merged_decisions[-1].source_id == decision.source_id
            and merged_decisions[-1].source_end_us == decision.source_start_us
        ):
            prior = merged_decisions[-1]
            merged = prior.model_copy(update={"source_end_us": decision.source_end_us})
            frames = max(1, (merged.source_end_us - merged.source_start_us) * fps // 1_000_000)
            merged_decisions[-1] = merged
            merged_minimum[-1] = merged_maximum[-1] = frames
            merged_preferred[-1] = float(frames)
        else:
            merged_decisions.append(decision)
            merged_minimum.append(low)
            merged_maximum.append(high)
            merged_preferred.append(weight)
    decisions, minimum, maximum, preferred = (
        merged_decisions,
        merged_minimum,
        merged_maximum,
        merged_preferred,
    )
    actual = max(sum(minimum), min(sum(maximum), plan.target_frame_count))
    counts = distribute(minimum, preferred, maximum, actual)
    result = plan.model_copy(
        update={
            "decisions": [
                decision.model_copy(update={"output_frame_count": count})
                for decision, count in zip(decisions, counts, strict=True)
            ],
            "actual_frame_count": actual,
            "settings": plan.settings | {"final_view_frames": final_frames},
            "warnings": plan.warnings
            + (["Final view shorter than requested"] if final_frames < desired_frames else [])
            + (
                ["Final view and speed limits make target infeasible"]
                if actual != plan.target_frame_count
                else []
            ),
        }
    )
    validate_plan(project, result)
    return result
