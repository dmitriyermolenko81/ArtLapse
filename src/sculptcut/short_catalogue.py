"""Full work verification before each Shorts-specific montage algorithm."""

import json
import math
from collections.abc import Callable
from pathlib import Path
from threading import Event

from sculptcut.budget import distribute
from sculptcut.domain import Project
from sculptcut.idle import IdleRange
from sculptcut.planning import RenderPlan, validate_plan
from sculptcut.project import atomic_write
from sculptcut.proxy import make_proxies
from sculptcut.segmentation import Segment
from sculptcut.shorts_layout import PHOTOSHOP_PRESETS
from sculptcut.shorts_quality import EpisodeInspector
from sculptcut.work_episodes import WorkEpisode, highlight_plan, work_catalogue

VERSION = "short-catalogue-266"
MINIMUM_SOURCE_US = 60_000_000


def complete_short_catalogue(
    root: Path, project: Project, base: RenderPlan, segments: list[Segment],
    cancel: Event | None = None, stage: Callable[[str], None] | None = None,
) -> RenderPlan:
    # Very short inputs retain the native small-source fallback. The full
    # source audit is intended for condensed recordings, as in timelapse265.
    if sum(s.duration_us for s in project.sources) < MINIMUM_SOURCE_US:
        return base
    drawing = project.settings.preset in PHOTOSHOP_PRESETS
    make_proxies(root, project, cancel)
    with EpisodeInspector(root, project, cancel, work_only=True, drawing_mode=drawing) as inspector:
        pool = work_catalogue(root, project, base, segments, inspector.development,
                              cancel, stage, use_features=not drawing)
    usable = [e for e in pool if e.process_verified]
    def save_catalogue() -> None:
        atomic_write(root / f"{project.settings.preset}-work-episodes.json", json.dumps({
            "version": VERSION, "candidates": [e.model_dump() | {"id": e.id} for e in pool],
        }))

    save_catalogue()
    settings = base.settings | {"complete_short_catalogue": VERSION,
        "short_catalogue_preset": project.settings.preset,
        "short_candidates_checked": len(pool), "short_candidates_verified": len(usable)}
    if not usable:
        # Unknown evidence remains unknown. Native modes still have a clean
        # final-view/static-input fallback; never claim these are verified edits.
        return base.model_copy(update={"settings": settings | {"short_catalogue_fallback": 1}})
    decisions = []
    for d in base.decisions:
        windows = [e for e in usable if e.source_id == d.source_id
                   and (e.work_start_us or e.start_us) < d.source_end_us
                   and (e.work_end_us or e.end_us) > d.source_start_us]
        bounds = {d.source_start_us, d.source_end_us}
        for e in windows:
            bounds.update(t for t in (e.work_start_us, e.work_end_us)
                          if t is not None and d.source_start_us < t < d.source_end_us)
        times = sorted(bounds)
        for a, b in zip(times, times[1:], strict=False):
            event = next((e for e in windows if (e.work_start_us or e.start_us) <= a
                      and b <= (e.work_end_us or e.end_us)), None)
            keep = d.action != "DROP" and (
                event is not None or d.provenance == "user:restored_work")
            decisions.append(d.model_copy(update={"source_start_us": a, "source_end_us": b,
                "action": "SPEED" if keep else "DROP", "output_frame_count": int(keep),
                "provenance": d.provenance if keep or d.action == "DROP"
                else "story:catalogue_omission",
                "episode_id": event.id if event else None,
                "episode_score": event.score if event else None}))
    caps = [max(1, math.floor((d.source_end_us-d.source_start_us)*base.output_fps/1e6))
            if d.output_frame_count else 0 for d in decisions]
    minimum = [int(c > 0) for c in caps]
    actual = max(sum(minimum), min(base.target_frame_count, sum(caps)))
    counts = distribute(minimum, [float(max(1, c)) for c in caps], caps, actual)
    result = base.model_copy(update={"decisions": [d.model_copy(update={"output_frame_count": n})
        for d, n in zip(decisions, counts, strict=True)], "actual_frame_count": actual,
        "settings": settings})
    validate_plan(project, result)
    from sculptcut.screen_cleanup import project_screens, strip_screens

    ranges = project_screens(root, project, cancel, result)
    rejected = {e.id for e in usable if any(r.source_id == e.source_id
        and r.start_us < (e.work_end_us or e.end_us)
        and r.end_us > (e.work_start_us or e.start_us) for r in ranges)}
    if rejected:
        full = [IdleRange(source_id=e.source_id, start_us=e.work_start_us or e.start_us,
                          end_us=e.work_end_us or e.end_us, reason="shorts:screen_overlay")
                for e in usable if e.id in rejected]
        result = strip_screens(project, result, full)
        pool = [e.model_copy(update={"process_verified": False,
                "process_note": "screen_overlay"}) if e.id in rejected else e for e in pool]
        save_catalogue()
        result = result.model_copy(update={"settings": result.settings | {
            "short_candidates_verified": len(usable)-len(rejected)}})
    return result


def catalogue_score(plan: RenderPlan, source_id: str, start: int, end: int) -> float:
    scores = [d.episode_score for d in plan.decisions if d.source_id == source_id
              and d.action != "DROP" and d.source_start_us < end and d.source_end_us > start
              and d.episode_score is not None]
    return 1 / (1 + max(scores, default=0))


def catalogue_process_plan(root: Path, project: Project, base: RenderPlan) -> RenderPlan:
    """Keep vetted work windows whole instead of cutting them into old chapters."""
    preset = base.settings.get("short_catalogue_preset", project.settings.preset)
    data = json.loads((root / f"{preset}-work-episodes.json").read_text())
    pool = [WorkEpisode.model_validate({k: v for k, v in e.items() if k != "id"})
            for e in data["candidates"]]
    base = base.model_copy(update={"settings": base.settings | {"preset": project.settings.preset}})
    plan = highlight_plan(project, base, pool)
    last = next(d for d in reversed(plan.decisions) if d.output_frame_count)
    decisions = [d.model_copy(update={"provenance": "user:shorts_final" if d is last else
        "user:process_story"}) if d.output_frame_count else d for d in plan.decisions]
    settings = {key: value for key, value in plan.settings.items()
                if key not in ("work_episodes_version", "work_process_gate")}
    settings.update({"story": "process", "bookends": True,
                     "final_view_frames": last.output_frame_count, "width": 1080, "height": 1920})
    result = plan.model_copy(update={"decisions": decisions, "settings": settings})
    validate_plan(project, result)
    return result
