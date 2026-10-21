"""Keep the work between highlights and bound uncertain introductory setup."""

import json
import math

from sculptcut.budget import distribute
from sculptcut.domain import Project
from sculptcut.planning import RenderDecision, RenderPlan, validate_plan
from sculptcut.segmentation import Segment


def bridge_minimum_frames(project: Project, capacity: int, fps: int) -> int:
    """Make a surviving process step readable without slowing it below 1x."""
    return max(math.ceil(capacity / min(project.settings.timelapse_bridge_speed_limit,
                                         project.settings.timelapse_episode_max_speed)),
               min(capacity, round(project.settings.timelapse_bridge_min_output_seconds * fps)))


def _bridge_windows(
    project: Project, decision: RenderDecision, segments: list[Segment],
) -> list[tuple[int, int]]:
    """Restore representative process steps, with a bounded decoding budget.

    Already omitted footage is sampled across time, rather than resurrected by
    one uncertain label. STATIC alone is not evidence of a missing process step.
    UNKNOWN/MIXED remain eligible, including when there is no confident work.
    """
    settings = project.settings
    interval = round(settings.timelapse_bridge_interval_seconds * 1_000_000)
    window = round(settings.timelapse_bridge_window_seconds * 1_000_000)
    eligible = [s for s in segments if s.source_id == decision.source_id
                and s.start_us < decision.source_end_us and s.end_us > decision.source_start_us
                and s.kind in ("SCULPT", "MIXED", "UNKNOWN", "FINAL_VIEW")]
    selected: list[tuple[int, int]] = []
    for left in range(decision.source_start_us, decision.source_end_us, interval):
        right = min(decision.source_end_us, left + interval)
        relevant = [s for s in eligible if s.start_us < right and s.end_us > left]
        if not relevant:
            continue
        length = min(window, right - left)
        starts = {max(left, min(right - length, anchor)) for s in relevant
                  for anchor in (s.start_us, s.end_us - length)}

        def rank(
            start: int, length: int = length, relevant: list[Segment] = relevant,
        ) -> tuple[float, int]:
            return (sum(max(0, min(start + length, s.end_us) - max(start, s.start_us))
                        * (2 if s.kind in ("SCULPT", "FINAL_VIEW") else 1)
                        for s in relevant), -start)

        start = max(starts, key=rank)
        hits = [s for s in relevant if s.start_us < start + length and s.end_us > start]
        end = min(start + length, max(s.end_us for s in hits))
        start = max(start, min(s.start_us for s in hits))
        if selected and selected[-1][1] == start:
            selected[-1] = selected[-1][0], end
        else:
            selected.append((start, end))
    return selected


def _split_bridge(
    project: Project, decision: RenderDecision, segments: list[Segment],
) -> list[RenderDecision]:
    if decision.action != "DROP" or decision.provenance != "story:episode_omission":
        return [decision]
    result = []
    position = decision.source_start_us
    for start, end in _bridge_windows(project, decision, segments):
        if start > position:
            result.append(decision.model_copy(update={
                "source_start_us": position, "source_end_us": start,
            }))
        result.append(decision.model_copy(update={
            "source_start_us": start, "source_end_us": end,
            "action": "SPEED", "provenance": "story:process_bridge",
        }))
        position = end
    if position < decision.source_end_us:
        result.append(decision.model_copy(update={"source_start_us": position}))
    return result


def preserve_work_bridges(
    project: Project, plan: RenderPlan, segments: list[Segment],
) -> RenderPlan:
    """A chapter winner is not evidence that the intervening work is disposable."""
    split = [part for d in plan.decisions for part in _split_bridge(project, d, segments)]
    if not any(d.provenance == "story:process_bridge" for d in split):
        return plan
    return balance_process_budget(project, plan.model_copy(update={"decisions": split}))


def balance_process_budget(project: Project, plan: RenderPlan) -> RenderPlan:
    """Pin secondary clips to a global budget; give all remaining time to episodes."""
    settings = project.settings
    fps = plan.output_fps
    caps = [max(1, (d.source_end_us-d.source_start_us)*fps//1_000_000)
            if d.action != "DROP" else 0 for d in plan.decisions]
    bridges = [i for i, d in enumerate(plan.decisions)
               if d.action != "DROP" and d.provenance == "story:process_bridge"]
    if not bridges:
        return plan
    minimum, maximum, weights = [], [], []
    for i, (d, cap) in enumerate(zip(plan.decisions, caps, strict=True)):
        fixed = d.provenance.startswith("user:") or d.provenance in (
            "story:outcome_hold", "story:setup_context",
        )
        low = min(cap, d.output_frame_count) if fixed else (math.ceil(
            (d.source_end_us-d.source_start_us)*fps
            / (1_000_000*settings.timelapse_episode_max_speed)) if cap else 0)
        minimum.append(0 if i in bridges else low)
        maximum.append(min(cap, d.output_frame_count) if fixed else cap)
        weights.append(float(max(1, d.output_frame_count)))
    budget = max(0, min(round(plan.target_frame_count * settings.timelapse_bridge_output_fraction),
                        plan.target_frame_count - sum(minimum)))
    speed = min(settings.timelapse_bridge_speed_limit, settings.timelapse_episode_max_speed)
    costs = {i: max(1, bridge_minimum_frames(project, caps[i], fps), math.ceil(
        (plan.decisions[i].source_end_us-plan.decisions[i].source_start_us)*fps
        / (1_000_000*speed))) for i in bridges}
    eligible = [i for i in bridges if costs[i] <= budget]
    selected: set[int] = set()
    if sum(costs[i] for i in eligible) <= budget:
        selected = set(eligible)
    elif eligible:
        # Bound the number of clips as well as their aggregate screen duration.
        # Temporal strata retain intermediate steps instead of only early winners.
        count = min(len(eligible), max(1, budget // max(costs[i] for i in eligible)))
        power = settings.timelapse_early_chapter_power
        for n in range(count):
            index = min(len(eligible)-1, int(len(eligible)*((n+0.5)/count)**power))
            available = [j for j in range(len(eligible)) if eligible[j] not in selected]
            chosen = min(available, key=lambda j: (abs(j-index), j))
            selected.add(eligible[chosen])
    decisions = list(plan.decisions)
    for i in bridges:
        if i in selected:
            minimum[i] = maximum[i] = costs[i]
        else:
            minimum[i] = maximum[i] = 0
            decisions[i] = decisions[i].model_copy(update={
                "action": "DROP", "output_frame_count": 0,
                "provenance": "story:episode_omission",
            })
    target = max(sum(minimum), min(plan.target_frame_count, sum(maximum)))
    counts = distribute(minimum, weights, maximum, target)
    result = plan.model_copy(update={
        "decisions": [d.model_copy(update={"output_frame_count": n})
                      for d, n in zip(decisions, counts, strict=True)],
        "actual_frame_count": target,
        "settings": plan.settings | {
            "process_bridge_source_us": sum(decisions[i].source_end_us
                                            - decisions[i].source_start_us for i in selected),
            "process_bridge_output_frames": sum(counts[i] for i in selected),
        },
    })
    validate_plan(project, result)
    return result


def limit_setup_context(
    project: Project, plan: RenderPlan, segments: list[Segment],
) -> RenderPlan:
    """Keep only a short lead-in before the first sustained work evidence.

    This does not claim to recognize Blender UI or delete uncertain model edits.
    Only an early, sustained work run enables this bound, and manual keeps win.
    """
    settings = project.settings
    cutoffs: dict[str, int] = {}
    for source in project.sources:
        starts = [s.start_us for s in segments if s.source_id == source.id
                  and s.kind == "SCULPT" and s.score >= 0.5
                  and s.end_us - s.start_us >= settings.timelapse_setup_work_seconds * 1_000_000
                  and s.start_us <= settings.timelapse_setup_search_seconds * 1_000_000]
        if starts:
            cutoffs[source.id] = min(starts)
    decisions, setup = [], []
    for decision in plan.decisions:
        cutoff = cutoffs.get(decision.source_id, 0)
        points = sorted({decision.source_start_us, decision.source_end_us}
                        | ({cutoff} if decision.source_start_us < cutoff
                           < decision.source_end_us else set()))
        for start, end in zip(points, points[1:], strict=False):
            count = (max(1, round(decision.output_frame_count * (end - start)
                                 / (decision.source_end_us - decision.source_start_us)))
                     if decision.action != "DROP" else 0)
            decisions.append(decision.model_copy(update={
                "source_start_us": start, "source_end_us": end, "output_frame_count": count,
            }))
            setup.append(decision.action != "DROP" and end <= cutoff
                         and not decision.provenance.startswith("user:"))
    if not any(setup):
        return plan
    for source_id in cutoffs:
        remaining = round(settings.timelapse_setup_context_seconds * 1_000_000)
        for i in range(len(decisions)-1, -1, -1):
            d = decisions[i]
            if not setup[i] or d.source_id != source_id:
                continue
            length = d.source_end_us-d.source_start_us
            keep = min(length, remaining)
            remaining -= keep
            if keep == length:
                continue
            drop = d.model_copy(update={
                "source_end_us": d.source_end_us-keep, "action": "DROP",
                "output_frame_count": 0, "provenance": "story:setup_trim",
            })
            if keep:
                tail = d.model_copy(update={"source_start_us": d.source_end_us-keep,
                                           "output_frame_count": max(1, round(
                                               keep*plan.output_fps/1_000_000))})
                decisions[i:i+1] = [drop, tail]
                setup[i:i+1] = [False, True]
            else:
                decisions[i] = drop
                setup[i] = False
    minimum, maximum, weights = [], [], []
    for decision in decisions:
        cap = (max(1, (decision.source_end_us - decision.source_start_us)
                   * plan.output_fps // 1_000_000) if decision.action != "DROP" else 0)
        fixed = (decision.provenance.startswith("user:")
                 or decision.provenance == "story:outcome_hold")
        speed = (settings.timelapse_bridge_max_speed
                 if decision.provenance == "story:process_bridge"
                 else settings.timelapse_episode_max_speed)
        low = min(cap, decision.output_frame_count) if fixed else math.ceil(cap / speed)
        if decision.provenance == "story:process_bridge":
            low = max(low, bridge_minimum_frames(project, cap, plan.output_fps))
        minimum.append(low)
        maximum.append(min(cap, decision.output_frame_count) if fixed else cap)
        weights.append(float(max(1, decision.output_frame_count)))
    for source_id in cutoffs:
        indices = [i for i, (d, intro) in enumerate(zip(decisions, setup, strict=True))
                   if intro and d.source_id == source_id]
        if not indices:
            continue
        # Keep at least one frame per surviving fragment; never resurrect a drop.
        budget = max(
            len(indices), round(settings.timelapse_setup_context_seconds * plan.output_fps)
        )
        budget = min(budget, sum(decisions[i].output_frame_count for i in indices))
        counts = distribute([1] * len(indices), [weights[i] for i in indices],
                            [maximum[i] for i in indices], budget)
        for i, count in zip(indices, counts, strict=True):
            minimum[i] = maximum[i] = count
            decisions[i] = decisions[i].model_copy(update={"provenance": "story:setup_context"})
    target = max(sum(minimum), min(plan.target_frame_count, sum(maximum)))
    counts = distribute(minimum, weights, maximum, target)
    result = plan.model_copy(update={
        "decisions": [d.model_copy(update={"output_frame_count": n})
                      for d, n in zip(decisions, counts, strict=True)],
        "actual_frame_count": target,
        "settings": plan.settings | {
            "setup_context_cutoffs_json": json.dumps(cutoffs, sort_keys=True),
        },
    })
    validate_plan(project, result)
    return result
