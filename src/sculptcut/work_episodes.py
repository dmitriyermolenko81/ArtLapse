"""Detect work throughout a recording before choosing short chronological highlights."""

import json
import math
from bisect import bisect_right
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Event

from sculptcut.analysis import FrameFeature
from sculptcut.budget import distribute
from sculptcut.development import (
    DevelopmentEvidence,
    DevelopmentScorer,
    development_key,
    development_weight,
)
from sculptcut.domain import Model, Project
from sculptcut.media import Cancelled
from sculptcut.planning import RenderDecision, RenderPlan, validate_plan
from sculptcut.project import (
    AnalysisCache,
    atomic_write,
    cache_get,
    cache_key,
    cache_put,
    cache_session,
)
from sculptcut.segmentation import Segment


@dataclass(frozen=True)
class EpisodeSettings:
    minimum_process_score: float = 0.04
    camera_confidence: float = 0.7
    source_seconds: float = 6
    minimum_source_seconds: float = 1.2
    preferred_output_seconds: float = 1.4
    maximum_output_seconds: float = 3
    minimum_output_seconds: float = 0.5
    preferred_speed: float = 2.4
    shortlist_per_interval: int = 2
    quiet_samples: int = 4
    quiet_change: float = 0.00002
    quiet_edges: float = 0.00002
    quiet_pixels: float = 0.00005
    change_scale: float = 0.004
    edge_scale: float = 0.01
    low_change_weight: float = 0.35
    uncertain_weight: float = 0.65
    camera_weight: float = 0.3
    ramp_strength: float = 0.3


SETTINGS = EpisodeSettings()
EPISODE_VERSION = "work-episodes-260-all-source-candidates"


class WorkEpisode(Model):
    process_strength: float | None = None
    process_checked: bool = False
    process_note: str = "uninspected"
    process_verified: bool | None = None
    work_start_us: int | None = None
    work_end_us: int | None = None
    source_id: str
    start_us: int
    end_us: int
    kind: str
    score: float
    samples: int = 0
    observed_change: float | None = None
    peak_us: int | None = None

    @property
    def id(self) -> str:
        return f"{self.source_id}:{self.start_us}:{self.end_us}"


@dataclass
class _Run:
    source_id: str
    start_us: int
    end_us: int
    kind: str
    score: float


def detect_episodes(
    project: Project,
    base: RenderPlan,
    segments: list[Segment],
    features: Iterable[FrameFeature] = (),
) -> list[WorkEpisode]:
    """An uncapped candidate catalogue, independent of the requested export time.

    Activity boundaries separate actions from pauses/navigation. Continuous work
    gets bounded windows, rather than one winner from a many-minute chapter.
    Features are consumed once; only scalar aggregates are kept in memory.
    """
    runs: list[_Run] = []
    by_source: dict[str, list[Segment]] = {source.id: [] for source in project.sources}
    for segment in segments:
        by_source[segment.source_id].append(segment)
    ends: dict[str, list[int]] = {}
    for sid, rows in by_source.items():
        rows.sort(key=lambda s: s.start_us)
        ends[sid] = [s.end_us for s in rows]
    for d in base.decisions:
        if d.action == "DROP":
            continue
        rows = by_source[d.source_id]
        position = d.source_start_us
        relevant = []
        for s in rows[bisect_right(ends[d.source_id], position) :]:
            if s.start_us >= d.source_end_us:
                break
            if s.start_us > position:
                relevant.append((position, s.start_us, "UNKNOWN", 0.5))
            a, b = max(position, s.start_us), min(d.source_end_us, s.end_us)
            confirmed_pause = (
                s.kind == "STATIC"
                and s.score >= project.settings.timelapse_static_min_score
                and s.end_us - s.start_us
                >= project.settings.timelapse_static_cut_seconds * 1e6
            )
            confirmed_ui = s.kind in ("INTERFACE", "PROCESSING") and s.score >= 0.9
            confirmed_camera = s.kind == "CAMERA" and s.score >= SETTINGS.camera_confidence
            if b > a and not confirmed_pause and not confirmed_ui and not confirmed_camera:
                kind = "SCULPT" if s.kind == "SCULPT" else (
                    "CAMERA" if s.kind == "CAMERA" else "UNKNOWN"
                )
                weight = 1 if kind == "SCULPT" else (
                    SETTINGS.camera_weight if kind == "CAMERA" else SETTINGS.uncertain_weight
                )
                relevant.append((a, b, kind, weight * (0.5 + s.importance)))
            position = max(position, b)
        if position < d.source_end_us:
            relevant.append((position, d.source_end_us, "UNKNOWN", 0.5))
        for a, b, kind, score in relevant:
            if (runs and runs[-1].source_id == d.source_id and runs[-1].end_us == a
                    and (runs[-1].kind == kind or min(
                        a - runs[-1].start_us, b - a
                    ) < SETTINGS.minimum_source_seconds * 1e6)):
                old = runs[-1]
                old.score = (old.score * (a - old.start_us) + score * (b - a)) / (
                    b - old.start_us
                )
                old.end_us = b
                if kind == "SCULPT":
                    old.kind = kind
            else:
                runs.append(_Run(d.source_id, a, b, kind, score))
    pool = []
    for run in runs:
        n = max(1, math.ceil((run.end_us - run.start_us) / (SETTINGS.source_seconds * 1e6)))
        for i in range(n):
            a = run.start_us + (run.end_us - run.start_us) * i // n
            b = run.start_us + (run.end_us - run.start_us) * (i + 1) // n
            pool.append(WorkEpisode(
                source_id=run.source_id, start_us=a, end_us=b, kind=run.kind, score=run.score,
            ))
    indices: dict[str, list[int]] = {s.id: [] for s in project.sources}
    for i, episode in enumerate(pool):
        indices[episode.source_id].append(i)
    cursor = dict.fromkeys(indices, 0)
    # count, change sum, edge sum, peak change, peak timestamp, quiet count
    aggregates: dict[int, list[float]] = {}
    for feature in features:
        source_indices = indices[feature.source_id]
        j = cursor[feature.source_id]
        while j < len(source_indices) and pool[source_indices[j]].end_us <= feature.timestamp_us:
            j += 1
        cursor[feature.source_id] = j
        if j == len(source_indices) or feature.timestamp_us < pool[source_indices[j]].start_us:
            continue
        if feature.measurement_quality < 0.9:
            continue
        values = aggregates.setdefault(source_indices[j], [0, 0, 0, -1, 0, 0])
        values[0] += 1
        values[1] += feature.roi_change
        values[2] += feature.edge_change
        if feature.roi_change > values[3]:
            values[3:5] = [feature.roi_change, feature.timestamp_us]
        values[5] += (
            feature.roi_change <= SETTINGS.quiet_change
            and feature.edge_change <= SETTINGS.quiet_edges
            and feature.changed_pixel_ratio <= SETTINGS.quiet_pixels
        )
    result = []
    for i, episode in enumerate(pool):
        measured = aggregates.get(i)
        if measured is None:
            result.append(episode)
            continue
        count, change, edges, _, peak, quiet = measured
        if count >= SETTINGS.quiet_samples and quiet == count:
            continue
        signal = min(
            1.0, change / count / SETTINGS.change_scale + edges / count / SETTINGS.edge_scale
        )
        result.append(episode.model_copy(update={
            "samples": int(count), "observed_change": change / count, "peak_us": int(peak),
            "score": episode.score * (
                SETTINGS.low_change_weight + (1 - SETTINGS.low_change_weight) * signal
            ),
        }))
    return result


def _choose(
    project: Project, pool: list[WorkEpisode], count: int,
    development: DevelopmentScorer | None,
) -> list[WorkEpisode]:
    offsets = {s.id: s.global_start_us for s in project.sources}
    def time(e: WorkEpisode) -> int:
        return offsets[e.source_id] + (e.start_us + e.end_us) // 2

    first, last = time(pool[0]), time(pool[-1])
    groups: dict[int, list[WorkEpisode]] = {}
    for e in pool:
        group = min(count - 1, (time(e) - first) * count // max(1, last - first + 1))
        groups.setdefault(group, []).append(e)
    chosen: dict[str, WorkEpisode] = {}
    prior: dict[str, DevelopmentEvidence] = {}
    for rows in groups.values():
        shortlist = sorted(rows, key=lambda e: (-e.score, e.start_us))[
            :SETTINGS.shortlist_per_interval
        ]
        best, evidence = shortlist[0], DevelopmentEvidence()
        best_score = -1.0
        for e in shortlist:
            observed = (
                development(e.source_id, e.start_us, e.end_us)
                if development else DevelopmentEvidence()
            )
            score = e.score * development_weight(observed, prior.get(e.source_id))
            if observed.score is not None:
                score *= SETTINGS.low_change_weight + observed.score
            if score > best_score:
                best, evidence, best_score = e, observed, score
        chosen[best.id] = best
        prior[best.source_id] = evidence
    # Empty time strata are not permission to shorten the target or stretch a
    # few winners. Fill from the other detected actions, still in chronology.
    for e in sorted(pool, key=lambda e: (-e.score, time(e))):
        if len(chosen) >= count:
            break
        chosen.setdefault(e.id, e)
    # Keep the last observed work in each recording, including the late tail.
    for source in project.sources:
        late = [e for e in pool if e.source_id == source.id and e.kind == "SCULPT"]
        if late:
            e = late[-1]
            if e.id not in chosen:
                alternatives = [v for v in chosen.values() if v.source_id == e.source_id]
                if alternatives:
                    removed = max(alternatives, key=time)
                    chosen.pop(removed.id)
                    chosen[e.id] = e
    return sorted(chosen.values(), key=time)


def highlight_plan(
    project: Project, base: RenderPlan, pool: list[WorkEpisode],
    development: DevelopmentScorer | None = None,
) -> RenderPlan:
    catalogue = pool
    pool = [e for e in pool if e.process_verified is not False]
    if not pool:
        if catalogue:
            first = next(d for d in base.decisions if d.output_frame_count)
            stop = min(first.source_end_us, first.source_start_us + math.ceil(1e6/base.output_fps))
            fallback = []
            for d in base.decisions:
                if d is first:
                    fallback.append(d.model_copy(update={"source_end_us": stop,
                        "output_frame_count": 1, "speed_ramp": None}))
                    if stop < d.source_end_us:
                        fallback.append(d.model_copy(update={"source_start_us": stop,
                            "action": "DROP", "output_frame_count": 0}))
                else:
                    fallback.append(d.model_copy(update={
                        "action": "DROP", "output_frame_count": 0}))
            return base.model_copy(update={"decisions": fallback, "actual_frame_count": 1,
                "settings": base.settings | {"work_verified_count": 0},
                "warnings": ["No confirmed work; brief source context only"]})
        return base
    fps, budget = base.output_fps, base.target_frame_count
    count = min(
        len(pool), budget,
        max(1, math.ceil(budget / (fps * SETTINGS.preferred_output_seconds))),
    )
    selected = _choose(project, pool, count, development)
    def natural(e: WorkEpisode) -> int:
        a = e.work_start_us if e.work_start_us is not None else e.start_us
        b = e.work_end_us if e.work_end_us is not None else e.end_us
        return max(1, (b - a) * fps // 1_000_000)

    ceiling = round(SETTINGS.maximum_output_seconds * fps)
    chosen_ids = {e.id for e in selected}
    capacity = sum(min(natural(e), ceiling) for e in selected)
    for e in sorted(pool, key=lambda e: (-e.score, e.start_us)):
        if capacity >= budget:
            break
        if e.id not in chosen_ids:
            selected.append(e)
            chosen_ids.add(e.id)
            capacity += min(natural(e), ceiling)
    offsets = {s.id: s.global_start_us for s in project.sources}
    selected.sort(key=lambda e: offsets[e.source_id] + e.start_us)
    maximum = [min(natural(e), ceiling) for e in selected]
    if sum(maximum) < budget:
        # Little source compression: use the actual work at 1x rather than
        # duplicate frames. This fallback is bounded by each original action.
        maximum = [natural(e) for e in selected]
        ceiling = max(maximum)
    target = min(budget, sum(maximum))
    floor = min(round(SETTINGS.minimum_output_seconds * fps), max(1, target // len(selected)))
    minimum = [min(floor, cap) for cap in maximum]
    counts = distribute(minimum, [
        max(1.0, natural(e) / SETTINGS.preferred_speed * (0.5 + e.score)) for e in selected
    ], maximum, target)
    windows: dict[str, tuple[WorkEpisode, int]] = {}
    for e, frames in zip(selected, counts, strict=True):
        left, right = e.work_start_us or e.start_us, e.work_end_us or e.end_us
        length = min(right - left, max(1, math.ceil(
            frames * min(project.settings.timelapse_episode_max_speed,
                         SETTINGS.preferred_speed) * 1e6 / fps
        )))
        if e.process_verified:
            length = right - left
        anchor = e.peak_us if e.peak_us is not None else (e.start_us + e.end_us) // 2
        a = max(left, min(right - length, anchor - length // 2))
        windows[e.id] = (e.model_copy(update={"start_us": a, "end_us": a + length}), frames)
    decisions: list[RenderDecision] = []
    source_pool: dict[str, list[WorkEpisode]] = {s.id: [] for s in project.sources}
    for e in catalogue:
        source_pool[e.source_id].append(e)
    for d in base.decisions:
        relevant = [e for e in source_pool[d.source_id]
                    if e.start_us < d.source_end_us and e.end_us > d.source_start_us]
        bounds = {d.source_start_us, d.source_end_us}
        for e in relevant:
            bounds.update((max(d.source_start_us, e.start_us), min(d.source_end_us, e.end_us)))
            bounds.update(t for t in (e.work_start_us, e.work_end_us)
                          if t is not None and d.source_start_us < t < d.source_end_us)
            if e.id in windows:
                w, _ = windows[e.id]
                bounds.update(
                    t for t in (w.start_us, w.end_us) if d.source_start_us < t < d.source_end_us
                )
        ordered = sorted(bounds)
        for a, b in zip(ordered, ordered[1:], strict=False):
            event = next((e for e in relevant if e.start_us <= a < b <= e.end_us), None)
            window = windows.get(event.id) if event else None
            keep = (d.action != "DROP" and window is not None
                    and window[0].start_us <= a < b <= window[0].end_us)
            n = 0
            if keep and window is not None:
                w, frames = window
                n = (b - w.start_us) * frames // (w.end_us - w.start_us) - (
                    (a - w.start_us) * frames // (w.end_us - w.start_us)
                )
            decisions.append(d.model_copy(update={
                "source_start_us": a, "source_end_us": b, "action": "SPEED" if n else "DROP",
                "output_frame_count": n, "episode_id": event.id if event else None,
                "episode_score": event.score if event else None,
                "speed_ramp": None, "subject_camera": None,
                "provenance": "story:work_highlight" if n else (
                    d.provenance if d.action == "DROP" else
                    "story:unverified_work" if event and (event.process_verified is False or
                        (event.work_start_us is not None and not (
                            event.work_start_us <= a < b <= (event.work_end_us or event.end_us)
                        ))) else
                    "story:work_candidate_omission"
                ),
            }))
    result = base.model_copy(update={
        "decisions": decisions, "actual_frame_count": sum(d.output_frame_count for d in decisions),
        "warnings": [], "settings": base.settings | {
            "story": "phrased_timelapse", "work_episodes_version": EPISODE_VERSION,
            "work_candidate_count": len(catalogue), "work_selected_count": len(selected),
            "work_verified_count": len(pool),
            "work_maximum_output_seconds": ceiling / fps,
            "highlight_ramp_strength": SETTINGS.ramp_strength,
            "phrase_max_speed": project.settings.timelapse_episode_max_speed,
        },
    })
    validate_plan(project, result)
    return result


def refill_highlights(
    project: Project, plan: RenderPlan, *, allow_new: bool = True,
) -> RenderPlan:
    """Fill edits from other actions instead of lengthening the few survivors.

    Only editorial candidate omissions may return. User exclusions and quality
    rejections stay excluded. The cap applies to the complete original action,
    including pieces split by output cuts or cleanup.
    """
    if not plan.settings.get("work_episodes_version"):
        return plan
    fps = plan.output_fps
    groups: dict[str, list[int]] = {}
    active: set[str] = set()
    active_ids = {d.episode_id for d in plan.decisions if d.output_frame_count}
    fixed = 0
    for i, d in enumerate(plan.decisions):
        automatic = d.provenance == "story:work_highlight"
        omitted = (d.provenance == "story:work_candidate_omission" and allow_new
                   and d.episode_id not in active_ids)
        if d.episode_id is not None and (automatic or omitted):
            groups.setdefault(d.episode_id, []).append(i)
            if d.output_frame_count:
                active.add(d.episode_id)
        else:
            fixed += d.output_frame_count
    ceiling = round(float(plan.settings.get(
        "work_maximum_output_seconds", SETTINGS.maximum_output_seconds
    )) * fps)
    caps = {
        eid: min(ceiling, sum(max(1, (plan.decisions[i].source_end_us
            - plan.decisions[i].source_start_us) * fps // 1_000_000) for i in indices))
        for eid, indices in groups.items()
    }
    current = {
        eid: sum(plan.decisions[i].output_frame_count for i in indices)
        for eid, indices in groups.items()
    }
    target = max(0, plan.target_frame_count - fixed)
    desired_count = min(len(groups), math.ceil(target / (fps * SETTINGS.preferred_output_seconds)))
    if (sum(current.values()) == target and len(active) >= desired_count
            and all(current[eid] <= caps[eid] for eid in active)):
        return plan
    chosen = sorted(active, key=lambda eid: groups[eid][0])
    candidates = sorted(
        (eid for eid in groups if eid not in active),
        key=lambda eid: (-(plan.decisions[groups[eid][0]].episode_score or 0), groups[eid][0]),
    )
    preferred = round(SETTINGS.preferred_output_seconds * fps)
    capacity = sum(min(caps[eid], preferred) for eid in chosen)
    for eid in candidates:
        if capacity >= target:
            break
        chosen.append(eid)
        capacity += min(caps[eid], preferred)
    chosen.sort(key=lambda eid: groups[eid][0])
    target = min(target, sum(caps[eid] for eid in chosen))
    if not chosen or target == 0:
        return plan
    counts = distribute(
        [0] * len(chosen),
        [float(max(1, current[eid] or round(SETTINGS.preferred_output_seconds * fps)))
         for eid in chosen],
        [caps[eid] for eid in chosen], target,
    )
    by_index: dict[int, int] = {}
    for eid, frames in zip(chosen, counts, strict=True):
        indices = groups[eid]
        natural = [max(1, (plan.decisions[i].source_end_us - plan.decisions[i].source_start_us)
                       * fps // 1_000_000) for i in indices]
        pieces = distribute([0] * len(indices), [float(n) for n in natural], natural, frames)
        by_index.update(zip(indices, pieces, strict=True))
    eligible = {i for indices in groups.values() for i in indices}
    decisions = []
    for i, d in enumerate(plan.decisions):
        if i not in eligible:
            decisions.append(d)
            continue
        count = by_index.get(i, 0)
        end = d.source_end_us
        if count and not plan.settings.get("work_process_gate"):
            end = min(end, d.source_start_us + max(1, math.ceil(
                count * min(project.settings.timelapse_episode_max_speed,
                            SETTINGS.preferred_speed) * 1e6 / fps
            )))
        decisions.append(d.model_copy(update={
            "source_end_us": end, "output_frame_count": count,
            "action": "SPEED" if count else "DROP", "speed_ramp": None,
            "provenance": "story:work_highlight" if count else "story:work_candidate_omission",
        }))
        if end < d.source_end_us:
            decisions.append(d.model_copy(update={
                "source_start_us": end, "action": "DROP", "output_frame_count": 0,
                "provenance": "story:work_candidate_omission", "speed_ramp": None,
            }))
    result = plan.model_copy(update={
        "decisions": decisions, "actual_frame_count": fixed + target,
        "settings": plan.settings | {
            "work_refill": 260,
            "work_needs_quality_check": int(
                bool(plan.settings.get("work_needs_quality_check")) or any(
                    eid not in active and count for eid, count in zip(chosen, counts, strict=True)
                )
            ),
        },
    })
    validate_plan(project, result)
    return result


def refresh_highlights(
    root: Path, project: Project, plan: RenderPlan, cancel: Event | None = None,
) -> RenderPlan:
    """Check newly used footage with the same screen/idle gates as initial picks."""
    from sculptcut.screen_cleanup import project_screens, strip_screens
    from sculptcut.timelapse_cleanup import clean_timelapse


    for _ in range(3):
        before = {(d.source_id, d.source_start_us, d.source_end_us)
                  for d in plan.decisions if d.output_frame_count}
        updated = refill_highlights(project, plan)
        after = {(d.source_id, d.source_start_us, d.source_end_us)
                 for d in updated.decisions if d.output_frame_count}
        plan = check_process(root, project, updated, cancel)
        removed = plan.actual_frame_count != updated.actual_frame_count
        if after <= before and not removed and not plan.settings.get("work_needs_quality_check"):
            return plan
        plan = plan.model_copy(update={"settings": plan.settings | {"work_needs_quality_check": 0}})
        plan = strip_screens(project, plan, project_screens(root, project, cancel, plan))
        plan = clean_timelapse(root, project, plan, cancel)
    # If little usable footage survives, don't fill the target with long holds.
    return check_process(root, project, refill_highlights(project, plan, allow_new=False), cancel)


def check_process(root: Path, project: Project, plan: RenderPlan,
                  cancel: Event | None = None) -> RenderPlan:
    if plan.settings.get("work_process_gate"):
        from sculptcut.shorts_quality import EpisodeInspector

        with EpisodeInspector(root, project, cancel, work_only=True) as inspector:
            decisions = []
            for d in plan.decisions:
                if d.output_frame_count and d.provenance == "story:work_highlight":
                    score = process_score(root, project, inspector.development,
                                          d.source_id, d.source_start_us, d.source_end_us)
                    if score is None or score < SETTINGS.minimum_process_score:
                        d = d.model_copy(update={"action": "DROP", "output_frame_count": 0,
                            "speed_ramp": None, "provenance": "story:unverified_work"})
                decisions.append(d)
            plan = plan.model_copy(update={"decisions": decisions,
                "actual_frame_count": sum(d.output_frame_count for d in decisions)})

    return plan


def legacy_process_key(project: Project, sid: str, start: int, end: int) -> str:
    source = next(s for s in project.sources if s.id == sid)
    return cache_key("process-266-drawing" if project.settings.preset.startswith("photoshop")
                    else "process-264-overlays", development_key(), source.fingerprint,
                    project.rois.get(sid), project.settings.proxy_fps, start, end)


def process_cache_key(project: Project, sid: str, start: int, end: int) -> str:
    from sculptcut.proxy import proxy_key
    from sculptcut.shorts_quality import QUALITY
    from sculptcut.webcam import webcam_roi

    source = next(s for s in project.sources if s.id == sid)
    return cache_key('process-input-320', legacy_process_key(project, sid, start, end),
                     proxy_key(project, source), webcam_roi(project.settings, sid), asdict(QUALITY))


def cached_process_score(cache: AnalysisCache, project: Project, sid: str,
                         start: int, end: int) -> dict[str, float | None] | None:
    """Qualify legacy scores once; later analysis-setting changes must miss."""
    from sculptcut.webcam import webcam_roi

    stage = 'drawing_process' if project.settings.preset.startswith('photoshop') else 'work_process'
    window_id = f'{sid}:{start}:{end}'
    key = process_cache_key(project, sid, start, end)
    value = cache.get(stage, window_id, key)
    if value is None and webcam_roi(project.settings, sid) is None:
        from sculptcut.proxy import proxy_key

        source = next(s for s in project.sources if s.id == sid)
        # A legacy key did not encode proxy width. Admit it only when its
        # record was written after the still-current proxy generation.
        # INSERT OR REPLACE allocates a new SQLite rowid to each generation.
        compatible = cache.connection.execute(
            "SELECT 1 FROM cache p JOIN cache w ON w.rowid>p.rowid "
            "WHERE p.stage='proxy' AND p.source_id=? AND p.key=? "
            "AND w.stage=? AND w.source_id=? AND w.key=?",
            (sid, proxy_key(project, source), stage, window_id,
             legacy_process_key(project, sid, start, end))).fetchone()
        if compatible:
            value = cache.get(stage, window_id, legacy_process_key(project, sid, start, end))
            if value is not None:
                cache.put(stage, window_id, key, value)
    return None if value is None else {'score':
        float(value['score']) if value['score'] is not None else None}


def process_score(
    root: Path, project: Project, scorer: DevelopmentScorer,
    sid: str, start: int, end: int,
) -> float | None:
    key = process_cache_key(project, sid, start, end)
    window_id = f"{sid}:{start}:{end}"
    stage = "drawing_process" if project.settings.preset.startswith("photoshop") else "work_process"
    with cache_session(root) as cache:
        value = cached_process_score(cache, project, sid, start, end)
        if value is not None:
            return float(value["score"]) if value["score"] is not None else None
        score = scorer(sid, start, end).score
        cache.put(stage, window_id, key, {"score": score})
        return score


def work_catalogue(
    root: Path, project: Project, base: RenderPlan, segments: list[Segment],
    development: DevelopmentScorer | None, cancel: Event | None,
    stage: Callable[[str], None] | None = None,
    *, use_features: bool = True,
) -> list[WorkEpisode]:
    from sculptcut.source_catalogue import catalogue

    # Source cuts are shared. Suitability is evaluated below for this mode.
    pool = [WorkEpisode(source_id=sid, start_us=a, end_us=b, kind='UNKNOWN', score=.5)
            for sid, a, b in catalogue(root, project)]
    verified = []
    if development is not None:
        if cancel and cancel.is_set():
            raise Cancelled('Work episode verification cancelled')
        catalogue_key = cache_key('verified-catalogue-320', asdict(SETTINGS),
            [(e.id, process_cache_key(project, e.source_id, e.start_us, e.end_us)) for e in pool])
        catalogue_kind = 'drawing' if project.settings.preset.startswith('photoshop') else 'sculpt'
        saved = cache_get(root, 'verified_catalogue', catalogue_kind, catalogue_key)
        if saved is not None:
            if stage:
                stage(f'Используем сохранённый анализ эпизодов: {len(saved)} / {len(pool)}')
            return [WorkEpisode.model_validate(e) for e in saved]
        length_us = round(SETTINGS.preferred_output_seconds * SETTINGS.preferred_speed * 1e6)
        windows = {(e.source_id, start, start+min(length_us, e.end_us-e.start_us))
                   for e in pool for start in sorted({e.start_us,
                       (e.start_us+e.end_us-min(length_us, e.end_us-e.start_us))//2,
                       e.end_us-min(length_us, e.end_us-e.start_us)})}
        scores: dict[tuple[str, int, int], float | None] = {}
        with cache_session(root) as cache:
            for sid, start, end in windows:
                value = cached_process_score(cache, project, sid, start, end)
                if value is not None:
                    scores[sid, start, end] = value['score']
        if stage and scores:
            stage(f'Оценки окон из кэша: {len(scores)}; новых: {len(windows)-len(scores)}')
        offsets = {source.id: source.global_start_us for source in project.sources}
        count = max(1, math.ceil(base.target_frame_count / (
            base.output_fps * SETTINGS.preferred_output_seconds)))
        groups: dict[int, list[WorkEpisode]] = {}
        total = sum(source.duration_us for source in project.sources)
        for e in pool:
            position = offsets[e.source_id] + (e.start_us + e.end_us)//2
            groups.setdefault(min(count-1, position*count//max(1,total)), []).append(e)
        for rows in groups.values():
            rows.sort(key=lambda e: -e.score)
        ordered = [rows[rank] for rank in range(max(map(len, groups.values()), default=0))
                   for rows in groups.values() if rank < len(rows)]
        inspected: dict[str, WorkEpisode] = {}
        for index, e in enumerate(ordered):
            if cancel and cancel.is_set():
                raise Cancelled("Work episode verification cancelled")
            if stage and len(scores) < len(windows) and index % 32 == 0:
                stage(f"Проверяем рабочие эпизоды: {index} / {len(ordered)}")
            length = min(length_us, e.end_us - e.start_us)
            starts = sorted({e.start_us, (e.start_us + e.end_us - length)//2, e.end_us-length})
            best: tuple[float, int] | None = None
            measured = False
            for start in starts:
                window = e.source_id, start, start+length
                score = scores[window] if window in scores else process_score(
                    root, project, development, *window)
                measured = measured or score is not None
                if score is not None and score >= SETTINGS.minimum_process_score:
                    if best is None or score > best[0]:
                        best = (score, start)
            inspected[e.id] = e.model_copy(update={"process_checked": True,
                "process_strength": best[0] if best else None,
                "process_note": "confirmed_change" if best else (
                    "no_persistent_change" if measured else "incomparable_or_occluded"),
                "process_verified": best is not None,
                "score": e.score * (0.5 + best[0]) if best else e.score,
                "work_start_us": best[1] if best else None,
                "work_end_us": best[1]+length if best else None,
                "peak_us": best[1]+length//2 if best else e.peak_us})
        verified = [inspected.get(e.id, e.model_copy(update={"process_verified": False}))
                    for e in pool]
        pool = verified
        if stage:
            stage(f"Проверено рабочих эпизодов: {len(ordered)} / {len(ordered)}"
                  if len(scores) < len(windows) else
                  f'Используем сохранённый анализ эпизодов: {len(pool)} / {len(pool)}')
        if cancel and cancel.is_set():
            raise Cancelled('Work episode verification cancelled')
        cache_put(root, 'verified_catalogue', catalogue_kind, catalogue_key,
                  [e.model_dump() for e in pool])
    return pool


def project_highlights(
    root: Path, project: Project, base: RenderPlan, segments: list[Segment],
    development: DevelopmentScorer | None, cancel: Event | None,
    stage: Callable[[str], None] | None = None,
) -> RenderPlan:
    pool = work_catalogue(root, project, base, segments, development, cancel, stage)
    result = highlight_plan(project, base, pool)
    if result.settings.get("work_episodes_version"):
        result = result.model_copy(update={
            "settings": result.settings | {"work_process_gate": 263}})
    selected = {d.episode_id for d in result.decisions if d.output_frame_count}
    atomic_write(root / "work_episodes.json", json.dumps({
        "version": EPISODE_VERSION,
        "source_fingerprints": {s.id: s.fingerprint for s in project.sources},
        "rois": {sid: roi.model_dump() for sid, roi in project.rois.items()},
        "candidates": [
            e.model_dump() | {"id": e.id, "selected": e.id in selected} for e in pool
        ],
    }))
    return result
