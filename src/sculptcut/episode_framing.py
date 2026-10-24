"""Frame manually restored footage without reselecting or retiming the edit."""

from collections.abc import Callable
from pathlib import Path
from threading import Event

from sculptcut.domain import Project, Roi
from sculptcut.media import Cancelled
from sculptcut.planning import RenderDecision, RenderPlan, validate_plan
from sculptcut.shorts_layout import PHOTOSHOP_PRESETS, SHORTS_PRESETS, apply_square_layout

EPISODE_FRAMING_VERSION = 2


def _inherit_framing(base: RenderPlan, framed: list[RenderDecision]) -> RenderPlan:
    decisions = []
    for decision in base.decisions:
        hits = [d for d in framed if d.source_id == decision.source_id
                and d.source_start_us < decision.source_end_us
                and d.source_end_us > decision.source_start_us]
        bounds = sorted({decision.source_start_us, decision.source_end_us} | {
            max(decision.source_start_us, min(t, decision.source_end_us))
            for d in hits for t in (d.source_start_us, d.source_end_us)
        })
        duration = decision.source_end_us - decision.source_start_us
        for start, end in zip(bounds, bounds[1:], strict=False):
            view = next((d for d in hits if d.source_start_us <= start
                         and end <= d.source_end_us), None)
            count = (decision.output_frame_count * (end - decision.source_start_us) // duration
                     - decision.output_frame_count * (start - decision.source_start_us) // duration)
            values: dict[str, object] = {
                "source_start_us": start, "source_end_us": end, "output_frame_count": count,
            }
            if view is not None:
                values |= {"crop": view.crop, "subject_camera": view.subject_camera}
            decisions.append(decision.model_copy(update=values))
    return base.model_copy(update={"decisions": decisions})


def frame_restored_episodes(
    root: Path, project: Project, plan: RenderPlan, base: RenderPlan,
    cancel: Event, progress: Callable[[float], None],
) -> tuple[RenderPlan, RenderPlan]:
    """Only omitted, unframed ranges made visible by episode edits need tracking.

    The neutral layer receives the same source-time framing, including partial
    restores, so reopening, Undo/Redo and later exports do not lose the camera.
    """
    if project.settings.preset not in SHORTS_PRESETS:
        return plan, base
    dropped = [d for d in base.decisions if d.action == "DROP"]
    music_process = (project.settings.preset == "music_shorts"
                     and project.settings.shorts_layout != "square"
                     and plan.settings.get("story") == "music_episodes")
    legacy_music = (music_process and int(plan.settings.get("episode_framing_version", 0))
                    < EPISODE_FRAMING_VERSION)
    selected = {
        i for i, d in enumerate(plan.decisions)
        if d.action != "DROP"
        and ((d.crop == "full" and d.subject_camera is None) or legacy_music
             or (music_process and d.subject_camera is not None
                 and (d.subject_camera.height > d.subject_camera.viewport.height
                      or d.subject_camera.width > d.subject_camera.viewport.width)))
        and d.provenance in ("user:KEEP", "user:RETAIN", "user:SET_SPEED", "user:FORCE_MONTAGE")
        and any(old.source_id == d.source_id and old.source_start_us <= d.source_start_us
                and old.source_end_us >= d.source_end_us for old in dropped)
    }
    if not selected:
        return plan, base
    if cancel.is_set():
        raise Cancelled("Episode framing cancelled")
    # Retain full source coverage, but analyse only the newly visible ranges.
    masked = [
        d if i in selected else d.model_copy(update={"action": "DROP", "output_frame_count": 0})
        for i, d in enumerate(plan.decisions)
    ]
    count = sum(d.output_frame_count for d in masked)
    tracking = plan.model_copy(update={"decisions": masked, "actual_frame_count": count,
                                       "target_frame_count": count})
    if project.settings.shorts_layout == "square":
        tracking = apply_square_layout(project, tracking, root, cancel)
    elif project.settings.preset in PHOTOSHOP_PRESETS:
        from sculptcut.photoshop import reframe_photoshop

        tracking = reframe_photoshop(project, tracking, cancel, progress)
    else:
        from sculptcut.shorts import reframe_shorts

        tracking = reframe_shorts(root, project, tracking, cancel, progress,
                                  preserve_selection=True)
    if cancel.is_set():
        raise Cancelled("Episode framing cancelled")
    # Blank or unrecognized intervals must not shrink a restored process shot
    # into a fitted Full UI thumbnail. Reuse its known source viewport instead.
    for i in sorted(selected):
        d = tracking.decisions[i]
        tracked_camera = d.subject_camera
        oversized = (music_process and tracked_camera is not None
                     and (tracked_camera.height > tracked_camera.viewport.height
                          or tracked_camera.width > tracked_camera.viewport.width))
        if tracked_camera is not None and not oversized:
            continue
        from sculptcut.shorts import viewport_shorts_camera

        neighbors = [n for n in plan.decisions if n.source_id == d.source_id
                     and n.subject_camera is not None]
        original_neighbors = [n for n in neighbors if n.provenance not in (
            "user:KEEP", "user:RETAIN", "user:SET_SPEED", "user:FORCE_MONTAGE",
        )]
        neighbors = original_neighbors or neighbors
        closest = min(neighbors, key=lambda n: abs(n.source_start_us - d.source_start_us),
                      default=None)
        camera = closest.subject_camera if closest is not None else None
        viewport = camera.viewport if camera is not None else project.rois.get(
            d.source_id, Roi(x=0, y=0, width=1, height=1),
        )
        source = next(s for s in project.sources if s.id == d.source_id)
        width, height = source.width, source.height
        if source.rotation % 180 == 90:
            width, height = height, width
        aspect = int(plan.settings["width"]) / int(plan.settings["height"]) * height / width
        fallback = viewport_shorts_camera(
            viewport, [], aspect, d.source_start_us, camera.background if camera else 70,
        )
        tracking.decisions[i] = d.model_copy(update={"crop": viewport, "subject_camera": fallback})
    before = [(d.source_id, d.source_start_us, d.source_end_us, d.action, d.output_frame_count)
              for d in masked]
    after = [(d.source_id, d.source_start_us, d.source_end_us, d.action, d.output_frame_count)
             for d in tracking.decisions]
    if before != after:
        raise ValueError("Episode framing must preserve the selected footage and frame counts")
    decisions = [tracking.decisions[i] if i in selected else d
                 for i, d in enumerate(plan.decisions)]
    framed = [decisions[i] for i in sorted(selected)]
    settings = plan.settings | {"episode_framing_version": EPISODE_FRAMING_VERSION}
    result = plan.model_copy(update={"decisions": decisions, "warnings": tracking.warnings,
                                     "settings": settings})
    neutral = _inherit_framing(base, framed)
    neutral = neutral.model_copy(update={
        "settings": base.settings | {"episode_framing_version": EPISODE_FRAMING_VERSION},
    })
    validate_plan(project, result)
    validate_plan(project, neutral)
    return result, neutral
