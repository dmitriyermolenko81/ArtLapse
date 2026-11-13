"""Resolve a grey source range into a bounded selection of observable work."""

import json
import logging
import math
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from sculptcut.activity import _stable_view, activity_key, classify
from sculptcut.analysis import analysis_key, read_features
from sculptcut.budget import distribute
from sculptcut.domain import Override, Project
from sculptcut.media import MediaError
from sculptcut.motion import Motion
from sculptcut.planning import RenderPlan
from sculptcut.project import cache_key
from sculptcut.shorts_quality import EpisodeInspector


@dataclass(frozen=True)
class RestoreSettings:
    session_pause_us: int = 6_000_000
    episode_us: int = 6_000_000
    step_us: int = 2_000_000
    maximum_episodes: int = 3
    inspected_candidates: int = 24
    length_divisors: tuple[int, ...] = (1, 2, 4)
    minimum_work_fraction: float = 0.25


SETTINGS = RestoreSettings()
RESTORED_WORK_PRESETS = ("timelapse", "shorts", "music_shorts")


class NoReadableWork(ValueError):
    """Analysis is readable, but this old restore request has no usable scene."""


def find_work_windows(root: Path, project: Project, rule: Override) -> list[tuple[int, int]]:
    """Use the first work session, stopping at a sustained pause, not the whole gap."""
    source = next(s for s in project.sources if s.id == rule.source_id)
    path = root / "analysis.sqlite"
    if not path.is_file():
        raise ValueError("Для возврата работы нужен анализ проекта. Создайте монтаж заново.")
    step = round(1_000_000 / project.settings.proxy_fps)
    active: list[tuple[int, int, float]] = []
    previous = None
    anchor = None
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        motion_key = cache_key("motion-2", analysis_key(project, source))
        last_active = None
        for feature in read_features(root, project, source, rule.start_us, rule.end_us):
            row = connection.execute(
                "SELECT value FROM motion WHERE source_id=? AND key=? AND timestamp=?",
                (source.id, motion_key, feature.timestamp_us),
            ).fetchone()
            motion = Motion.model_validate_json(row[0]) if row else Motion()
            stable, next_anchor = _stable_view(feature, previous, anchor)
            anchor = next_anchor if motion.quality >= 0.5 and motion.displacement < 0.5 else None
            activity = classify(feature, motion, perceptually_static=stable and anchor is not None)
            previous = feature
            weight = (
                1.0
                if activity.kind == "SCULPT"
                else 0.25
                if activity.kind in ("MIXED", "UNKNOWN") and feature.roi_change > 0.00002
                else 0.0
            )
            if (
                last_active is not None
                and feature.timestamp_us - last_active >= SETTINGS.session_pause_us
            ):
                break
            if weight:
                end = min(rule.end_us, feature.timestamp_us + step)
                active.append((feature.timestamp_us, end, weight))
                last_active = end
    except sqlite3.Error as exc:
        raise ValueError("Нужен анализ для возврата работы. Создайте монтаж заново.") from exc
    finally:
        connection.close()
    if not active:
        raise NoReadableWork("В выбранном пропуске не найдена работа для возврата.")
    minimum = max(1, round(project.settings.beat_sync_min_shot_seconds * 1_000_000))
    start, end = active[0][0], active[-1][1]
    length = min(SETTINGS.episode_us, end - start)
    if length < minimum:
        raise NoReadableWork("В выбранном пропуске слишком короткий участок работы.")
    lengths = sorted(
        {max(minimum, length // divisor) for divisor in SETTINGS.length_divisors}, reverse=True
    )
    ranked = []
    quota = max(1, SETTINGS.inspected_candidates // len(lengths))
    for size in lengths:
        candidates = []
        for a in sorted(
            set(range(start, end - size + 1, min(SETTINGS.step_us, size))) | {end - size}
        ):
            b = a + size
            work = sum(max(0, min(b, y) - max(a, x)) * weight for x, y, weight in active)
            if work >= size * SETTINGS.minimum_work_fraction:
                candidates.append((work, a, b))
        ranked.extend(sorted(candidates, reverse=True)[:quota])
    scored = []
    try:
        with EpisodeInspector(root, project) as inspector:
            for work, a, b in ranked:
                quality = inspector.score(source.id, a, b)
                if quality > 0:
                    scored.append((work * quality, a, b))
    except MediaError as exc:
        raise ValueError("Нужен proxy для возврата работы. Создайте монтаж заново.") from exc
    windows: list[tuple[int, int]] = []
    for _, a, b in sorted(scored, key=lambda row: (-row[0], row[1])):
        if not any(x < b and y > a for x, y in windows):
            windows.append((a, b))
        if len(windows) == SETTINGS.maximum_episodes:
            break
    if not windows:
        raise NoReadableWork("В выбранном пропуске нет читаемого участка работы для возврата.")
    return sorted(windows)


def prepare_restored_work(
    root: Path | None,
    project: Project,
    plan: RenderPlan,
) -> tuple[Project, RenderPlan, bool]:
    rules = [o for o in project.overrides if o.action == "SET_SPEED" and o.restore_omitted
             and not o.exact_selection]
    if not rules or project.settings.preset not in RESTORED_WORK_PRESETS:
        return project, plan, False
    excluded = [
        (d.source_id, d.source_start_us, d.source_end_us)
        for d in plan.decisions
        if d.provenance == "user:shorts_manual_cut"
    ]
    key = cache_key(
        "restored-work-254",
        asdict(SETTINGS),
        [activity_key(project, s) for s in project.sources],
        [o.model_dump(mode="json") for o in rules],
        excluded,
        project.settings.beat_sync_min_shot_seconds,
    )
    cached = json.loads(str(plan.settings.get("restored_work_selection", "{}")))
    if cached.get("key") == key:
        selections = cached["selections"]
    else:
        if root is None:
            raise ValueError("Для возврата работы откройте эпизоды из проекта.")
        selections = []
        for rule in rules:
            try:
                windows = find_work_windows(root, project, rule)
            except NoReadableWork as exc:
                # A stale broad restore must not abort a fresh Create or force
                # an unreadable/idle block back into the new episode selection.
                windows = []
                logging.getLogger('artlapse').info(
                    'Restore request has no readable work; keeping automatic selection',
                    extra={'event': 'restore_fallback', 'source_id': rule.source_id,
                           'start_us': rule.start_us, 'end_us': rule.end_us, 'reason': str(exc)})
            windows = [
                (a, b)
                for a, b in windows
                if not any(
                    source_id == rule.source_id and x < b and y > a for source_id, x, y in excluded
                )
            ]
            selections.append(windows)
        cached = {"key": key, "selections": selections}
    replacements = [o for o in project.overrides if o not in rules]
    for rule, windows in zip(rules, selections, strict=True):
        if not windows:
            continue  # No forced restore and no synthetic DROP over the entire gap.
        position = rule.start_us
        for a, b in windows:
            if a > position:
                replacements.append(
                    rule.model_copy(update={"action": "DROP", "start_us": position, "end_us": a})
                )
            replacements.append(rule.model_copy(update={"start_us": a, "end_us": b}))
            position = b
        if position < rule.end_us:
            replacements.append(rule.model_copy(update={"action": "DROP", "start_us": position}))
    prepared = project.model_copy(update={"overrides": replacements})
    plan = plan.model_copy(
        update={
            "settings": plan.settings
            | {
                "restored_work_selection": json.dumps(cached),
            }
        }
    )
    return prepared, plan, any(selections)


def fit_edit_budget(project: Project, plan: RenderPlan, target: int) -> RenderPlan:
    """Trade source coverage for time rather than extending a corrected movie."""
    if any(d.action == "MONTAGE" for d in plan.decisions):
        from sculptcut.shorts_refill import _expand_montage

        plan = plan.model_copy(
            update={
                "decisions": [
                    part for decision in plan.decisions for part in _expand_montage(decision)
                ]
            }
        )
    active = [i for i, d in enumerate(plan.decisions) if d.output_frame_count]
    minimum = max(
        1, min(target, round(project.settings.beat_sync_min_shot_seconds * plan.output_fps))
    )
    capacity = {
        i: max(
            plan.decisions[i].output_frame_count,
            (plan.decisions[i].source_end_us - plan.decisions[i].source_start_us)
            * plan.output_fps
            // 1_000_000,
        )
        if plan.decisions[i].action == "HOLD"
        else max(
            1,
            (plan.decisions[i].source_end_us - plan.decisions[i].source_start_us)
            * plan.output_fps
            // 1_000_000,
        )
        for i in active
    }
    selected = active[:]
    if sum(min(minimum, capacity[i]) for i in selected) > target:
        count = max(1, target // minimum)
        # Keep restored work first, then cover the rest of the source chronology.
        priority = [i for i in active if plan.decisions[i].provenance == "user:restored_work"]
        priority += [
            i
            for i in active
            if plan.decisions[i].provenance
            in (
                "user:shorts_final",
                "user:final_tail",
            )
        ]
        selected = priority[:count]
        remaining = [i for i in active if i not in selected]
        slots = count - len(selected)
        selected += [remaining[k * len(remaining) // slots] for k in range(slots)] if slots else []
        if sum(capacity[i] for i in selected) < target:
            selected = sorted(active, key=lambda i: capacity[i], reverse=True)[:count]
            for i in sorted(active, key=lambda i: capacity[i], reverse=True)[count:]:
                if sum(capacity[j] for j in selected) >= target:
                    break
                selected.append(i)
            minimum = min(minimum, max(1, target // len(selected)))
        selected.sort()
    allocations = distribute(
        [min(minimum, capacity[i]) for i in selected],
        [float(plan.decisions[i].output_frame_count) for i in selected],
        [capacity[i] for i in selected],
        target,
    )
    counts = dict(zip(selected, allocations, strict=True))
    decisions = [
        d.model_copy(
            update={
                "output_frame_count": counts.get(i, 0),
                "action": d.action if i in counts else "DROP",
                "provenance": (
                    d.provenance
                    if d.action == "HOLD"
                    or d.provenance
                    in (
                        "user:restored_work",
                        "user:shorts_final",
                        "user:final_tail",
                    )
                    else "auto:budget_episode"
                )
                if i in counts
                else "auto:budget_omission",
            }
        )
        if i in active
        else d
        for i, d in enumerate(plan.decisions)
    ]
    settings = plan.settings | {"automatic_budget_fit": 1}
    if settings.get("bookends"):
        finale = 0
        for decision in reversed(decisions):
            if not decision.output_frame_count:
                continue
            if decision.provenance not in ("user:shorts_final", "user:final_tail"):
                break
            finale += decision.output_frame_count
        settings["final_view_frames"] = finale
        if settings.get("final_presentation_frames"):
            settings["final_presentation_frames"] = next(
                d.output_frame_count for d in reversed(decisions) if d.output_frame_count
            )
    return plan.model_copy(
        update={
            "decisions": decisions,
            "target_frame_count": target,
            "actual_frame_count": target,
            "warnings": [
                w
                for w in plan.warnings
                if w
                not in (
                    "Overrides make target infeasible",
                    "Target infeasible under speed/retention constraints",
                    "Target infeasible; preserved ranges at >=1x",
                )
            ],
            "settings": settings,
        }
    )


def trim_budget_windows(project: Project, plan: RenderPlan) -> RenderPlan:
    """After beat fitting, shorten source windows to satisfy playback speed limits."""
    from sculptcut.music_episodes import episode_speed_limit

    decisions = []
    for decision in plan.decisions:
        if not decision.output_frame_count or decision.action == "HOLD":
            decisions.append(decision)
            continue
        if project.settings.preset == "timelapse":
            speed = float(
                project.settings.timelapse_bridge_max_speed
                if "bridge" in decision.provenance
                else project.settings.timelapse_episode_max_speed
            )
        elif project.settings.preset == "speed":
            speed = max(
                1.0,
                (decision.source_end_us - decision.source_start_us)
                * plan.output_fps
                / (decision.output_frame_count * 1_000_000),
            )
        elif project.settings.preset.startswith("photoshop_"):
            from sculptcut.photoshop_story import STORY as DRAWING_STORY

            speed = float(DRAWING_STORY.max_speed)
        else:
            speed = float(episode_speed_limit(project))
        for rule in project.overrides:
            if (
                rule.action == "SET_SPEED"
                and rule.source_id == decision.source_id
                and rule.start_us <= decision.source_start_us
                and rule.end_us >= decision.source_end_us
            ):
                speed = min(speed, rule.speed)
        length = max(
            1,
            math.ceil(
                decision.output_frame_count * speed * 1_000_000 / plan.output_fps,
            ),
        )
        end = min(decision.source_end_us, decision.source_start_us + length)
        decisions.append(decision.model_copy(update={"source_end_us": end}))
        if end < decision.source_end_us:
            decisions.append(
                decision.model_copy(
                    update={
                        "source_start_us": end,
                        "action": "DROP",
                        "output_frame_count": 0,
                        "provenance": "auto:budget_omission",
                    }
                )
            )
    return plan.model_copy(update={"decisions": decisions})
