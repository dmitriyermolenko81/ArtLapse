"""Chronological context/work/result phrases with fast intermediate work."""

import math
from collections.abc import Callable
from dataclasses import dataclass

from sculptcut.budget import distribute
from sculptcut.development import DevelopmentScorer
from sculptcut.domain import Project, SpeedRamp
from sculptcut.planning import RenderPlan, validate_plan
from sculptcut.segmentation import Segment
from sculptcut.timelapse_story import _select_windows

ViewScorer = Callable[[str, int], tuple[float, bool] | None]


@dataclass(frozen=True)
class PhraseSettings:
    source_seconds: int = 60
    output_seconds: int = 12
    chapter_power: float = 1.1
    context_fraction: float = 0.22
    result_fraction: float = 0.2
    view_difference: float = 0.1
    bridge_seconds: int = 12
    bridges_per_phrase: int = 2
    minimum_bridge_seconds: float = 0.35
    context_speed: int = 24
    detail_speed: int = 4
    result_speed: int = 4
    ramp_strength: float = 0.65
    ramp_minimum_seconds: float = 0.5
    ramp_safety_speed: float = 1.1
    closing_search_seconds: int = 300
    closing_window_seconds: int = 60
    closing_minimum_work_seconds: float = 1.2
    closing_minimum_score: float = 0.5


SETTINGS = PhraseSettings()


def phrase_plan(
    project: Project,
    base: RenderPlan,
    segments: list[Segment],
    development: DevelopmentScorer | None = None,
    view: ViewScorer | None = None,
) -> RenderPlan:
    settings = project.settings
    selection = project.model_copy(
        update={
            "settings": settings.model_copy(
                update={
                    "timelapse_episode_source_seconds": max(
                        settings.timelapse_episode_source_seconds, SETTINGS.source_seconds
                    ),
                    "timelapse_episode_output_seconds": SETTINGS.output_seconds,
                    "timelapse_early_chapter_power": SETTINGS.chapter_power,
                }
            )
        }
    )
    windows = _select_windows(selection, base, segments, development)
    if not windows:
        return base
    # Keep a closing work phrase for each recording. The winner of the final
    # broad chapter may otherwise end many minutes before the actual result.
    for source in project.sources:
        ends = [
            min(s.end_us, d.source_end_us)
            for s in segments
            for d in base.decisions
            if s.source_id == d.source_id == source.id
            and d.action != "DROP"
            and s.kind == "SCULPT"
            and s.score >= SETTINGS.closing_minimum_score
            and min(s.end_us, d.source_end_us) - max(s.start_us, d.source_start_us)
            >= SETTINGS.closing_minimum_work_seconds * 1_000_000
            and s.end_us >= source.duration_us - SETTINGS.closing_search_seconds * 1_000_000
        ]
        prior = max((b for sid, _, b in windows if sid == source.id), default=0)
        if ends and max(ends) > prior:
            end = max(ends)
            start = max(prior, end - SETTINGS.closing_window_seconds * 1_000_000)
            if end - start >= SETTINGS.closing_minimum_work_seconds * 1_000_000:
                if len(windows) < settings.timelapse_episode_limit:
                    windows.append((source.id, start, end))
                else:
                    replace = next(
                        (i for i in reversed(range(len(windows))) if windows[i][0] == source.id),
                        None,
                    )
                    if replace is not None:
                        windows[replace] = source.id, start, end
    source_order = {s.id: i for i, s in enumerate(project.sources)}
    windows.sort(key=lambda row: (source_order[row[0]], row[1]))
    phases = []
    observed = 0
    for sid, a, b in windows:
        left = a + round((b - a) * SETTINGS.context_fraction)
        right = b - round((b - a) * SETTINGS.result_fraction)
        if view is not None:
            points = [a + (b - a - 1) * i // 5 for i in range(6)]
            evidence = [view(sid, t) for t in points]
            initial = evidence[0]
            choices = [
                (i, e)
                for i, e in enumerate(evidence)
                if i in (2, 3)
                and e is not None
                and initial is not None
                and e[0] > initial[0] + SETTINGS.view_difference
                and any(e is not None and e[1] for e in evidence[i + 1 :])
            ]
            if choices:
                focus = max(choices, key=lambda pair: pair[1][0])[0]
                left, right = points[focus - 1], points[focus + 1]
                observed += 1
        phases += [
            (sid, a, left, "context"),
            (sid, left, right, "detail"),
            (sid, right, b, "result"),
        ]
    # Intermediate steps get their own chronological strata, including the
    # latest work. They no longer compete for an early-biased 15% bridge budget.
    candidates = set()
    for d in base.decisions:
        if d.action == "DROP":
            continue
        for segment in segments:
            if segment.source_id != d.source_id or segment.kind not in (
                "SCULPT",
                "UNKNOWN",
                "MIXED",
                "FINAL_VIEW",
            ):
                continue
            a, b = max(d.source_start_us, segment.start_us), min(d.source_end_us, segment.end_us)
            if b <= a:
                continue
            length = min(SETTINGS.bridge_seconds * 1_000_000, b - a)
            a = (a + b - length) // 2
            b = a + length
            if not any(sid == d.source_id and x < b and y > a for sid, x, y in windows):
                candidates.add((d.source_id, a, b))
    order = {s.id: i for i, s in enumerate(project.sources)}
    bridges = sorted(candidates, key=lambda row: (order[row[0]], row[1]))
    count = min(len(bridges), max(1, len(windows) * SETTINGS.bridges_per_phrase))
    if count:
        offsets = {s.id: s.global_start_us for s in project.sources}
        times = [offsets[sid] + (a + b) // 2 for sid, a, b in bridges]
        chosen = set()
        for i in range(count):
            target = times[0] + (times[-1] - times[0]) * i / max(1, count - 1)
            chosen.add(
                min(
                    (j for j in range(len(bridges)) if j not in chosen),
                    key=lambda j: (abs(times[j] - target), j),
                )
            )
        bridges = [bridges[j] for j in sorted(chosen)]
    decisions, minimum, maximum, weights = [], [], [], []
    for d in base.decisions:
        bounds = {d.source_start_us, d.source_end_us}
        ranges = [(a, b) for sid, a, b, _ in phases if sid == d.source_id]
        ranges += [(a, b) for sid, a, b in bridges if sid == d.source_id]
        for a, b in ranges:
            if a < d.source_end_us and b > d.source_start_us:
                bounds.update((max(a, d.source_start_us), min(b, d.source_end_us)))
        static = [
            s
            for s in segments
            if s.source_id == d.source_id
            and s.kind == "STATIC"
            and s.score >= settings.timelapse_static_min_score
            and s.end_us - s.start_us >= settings.timelapse_static_cut_seconds * 1_000_000
        ]
        for s in static:
            if s.start_us < d.source_end_us and s.end_us > d.source_start_us:
                bounds.update((max(s.start_us, d.source_start_us), min(s.end_us, d.source_end_us)))
        ordered = sorted(bounds)
        for a, b in zip(ordered, ordered[1:], strict=False):
            role = next(
                (role for sid, x, y, role in phases if sid == d.source_id and x <= a < b <= y), None
            )
            bridge = any(sid == d.source_id and x <= a < b <= y for sid, x, y in bridges)
            keep = d.action != "DROP" and (role is not None or bridge)
            if any(s.start_us <= a < b <= s.end_us for s in static):
                keep = False
            cap = max(1, (b - a) * base.output_fps // 1_000_000) if keep else 0
            speed = (
                SETTINGS.context_speed
                if role == "context" or bridge
                else SETTINGS.result_speed
                if role == "result"
                else SETTINGS.detail_speed
            )
            limit = (
                settings.timelapse_bridge_max_speed
                if role == "context" or bridge
                else settings.timelapse_episode_max_speed
            )
            low = max(1, math.ceil(cap / limit)) if cap else 0
            if bridge and cap:
                low = min(cap, max(low, round(SETTINGS.minimum_bridge_seconds * base.output_fps)))
            decisions.append(
                d.model_copy(
                    update={
                        "source_start_us": a,
                        "source_end_us": b,
                        "action": "SPEED" if keep else "DROP",
                        "output_frame_count": low,
                        "provenance": f"story:phrase_{role}"
                        if keep and role
                        else "story:process_bridge"
                        if keep
                        else d.provenance
                        if d.action == "DROP"
                        else "story:phrase_omission",
                        "speed_ramp": None,
                        "subject_camera": None,
                    }
                )
            )
            minimum.append(low)
            maximum.append(cap)
            weights.append(max(1.0, cap / speed))
    if not sum(maximum):
        return base
    target = max(sum(minimum), min(base.target_frame_count, sum(maximum)))
    counts = distribute(minimum, weights, maximum, target)
    result = base.model_copy(
        update={
            "decisions": [
                d.model_copy(update={"output_frame_count": n})
                for d, n in zip(decisions, counts, strict=True)
            ],
            "actual_frame_count": target,
            "settings": base.settings
            | {
                "story": "phrased_timelapse",
                "phrase_count": len(windows),
                "phrase_observed_views": observed,
                "phrase_bridges": len(bridges),
                "phrase_max_speed": settings.timelapse_bridge_max_speed,
            },
            "warnings": [],
        }
    )
    validate_plan(project, result)
    return result


def smooth_phrases(plan: RenderPlan) -> RenderPlan:
    if plan.settings.get("story") != "phrased_timelapse":
        return plan
    decisions = []
    for d in plan.decisions:
        ramp = None
        if (
            d.action == "SPEED"
            and d.provenance in ("story:phrase_detail", "story:work_highlight")
            and d.output_frame_count >= SETTINGS.ramp_minimum_seconds * plan.output_fps
        ):
            mean = (
                (d.source_end_us - d.source_start_us)
                * plan.output_fps
                / (d.output_frame_count * 1e6)
            )
            strength = min(
                float(plan.settings.get("highlight_ramp_strength", SETTINGS.ramp_strength)),
                max(0.0, mean - SETTINGS.ramp_safety_speed),
                max(0.0, 1 - mean / float(plan.settings.get("phrase_max_speed", 128))),
            )
            if strength > 0:
                ramp = SpeedRamp(
                    start_us=d.source_start_us, end_us=d.source_end_us, strength=strength
                )
        decisions.append(d.model_copy(update={"speed_ramp": ramp}))
    return plan.model_copy(
        update={
            "decisions": decisions,
            "settings": plan.settings
            | {
                "speed_pacing": "smooth-259",
                "speed_ramp_count": sum(d.speed_ramp is not None for d in decisions),
            },
        }
    )
