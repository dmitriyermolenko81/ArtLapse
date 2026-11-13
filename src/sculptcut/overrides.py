"""Non-destructive user decisions applied only to the render plan."""

import math
from collections.abc import Sequence

from sculptcut.budget import distribute
from sculptcut.domain import Override, Project
from sculptcut.planning import RenderDecision, RenderPlan, validate_plan


def apply_overrides(
    project: Project, plan: RenderPlan, *, manual: bool = False,
    inherited_speeds: Sequence[Override] = (),
    bounded_restores: bool = False,
) -> RenderPlan:
    if not project.overrides:
        return plan
    stills = plan.settings.get("story") in ("beat_stills", "photoshop_beat_stills")
    musical = (not stills
               and project.settings.preset in ("music_shorts", "photoshop_music_shorts")
               and bool(plan.settings.get("story")))
    episodic = (musical or plan.settings.get("story") == "phrased_timelapse"
                or bool(plan.settings.get("complete_short_catalogue")))
    pieces: list[RenderDecision] = []
    minimum, maximum, preferred = [], [], []
    fps = plan.output_fps
    for decision in plan.decisions:
        relevant = [
            o
            for o in project.overrides
            if o.source_id == decision.source_id
            and o.start_us < decision.source_end_us
            and o.end_us > decision.source_start_us
            # Output trims are an independent exclusion layer. An episode's
            # speed/keep override must never resurrect an already cut interval.
            and decision.provenance != "user:shorts_manual_cut"
            and not (decision.provenance == "auto:budget_omission"
                     and o.action == "SET_SPEED" and not o.restore_omitted)
            # Speed controls do not mean "return all omitted source footage" in
            # a newly selected musical montage. Explicit episode edits still can.
            and not (episodic and decision.action == "DROP" and o.action == "SET_SPEED"
                     and ((not manual and not o.exact_selection) or (not o.restore_omitted and any(
                         old.source_id == o.source_id and old.start_us <= o.start_us
                         and o.end_us <= old.end_us and old.speed == o.speed
                         for old in inherited_speeds
                     ))))
            # Still frames have no playback speed; keep their musical duration.
            and (not stills or o.action == "DROP")
            # A verified final hold also has display time, not playback speed.
            # Reapplying a broad speed rule must not shorten that presentation.
            and not (decision.action == "HOLD" and o.action == "SET_SPEED")
        ]
        bounds = sorted(
            {decision.source_start_us, decision.source_end_us}
            | {
                max(decision.source_start_us, min(decision.source_end_us, t))
                for o in relevant
                for t in (o.start_us, o.end_us)
            }
        )
        for start, end in zip(bounds, bounds[1:], strict=False):
            override = next((o for o in relevant if o.start_us <= start and end <= o.end_us), None)
            from sculptcut.timewarp import output_before

            count = max(1,output_before(decision,end)-output_before(decision,start)
                        if decision.speed_ramp is not None else round(
                            decision.output_frame_count*(end-start)/(
                                decision.source_end_us-decision.source_start_us)))
            cap = max(1, math.floor((end - start) * fps / 1e6))
            action = decision.action
            fixed = decision.provenance == "user:final_tail" or decision.action == "HOLD"
            provenance = decision.provenance
            speed_ceiling = None
            if override:
                provenance = f"user:{override.action}"
                if decision.provenance == "auto:budget_episode" and override.action == "SET_SPEED":
                    provenance = decision.provenance
                fixed = True
                if override.action == "FORCE_MONTAGE":
                    action = "SPEED"
                if override.action == "DROP":
                    action, count = "DROP", 0
                elif override.action == "KEEP":
                    action, count = "KEEP", cap
                elif override.action == "RETAIN":
                    action, fixed = "SPEED", False
                elif override.action == "SET_SPEED":
                    action, count = "SPEED", max(1, math.floor(cap / override.speed))
                    if (bounded_restores and override.restore_omitted
                            and not override.exact_selection):
                        provenance = "user:restored_work"
                        fixed = False
                        speed_ceiling = override.speed
                        count = max(1, math.ceil(cap / override.speed))
            if action == "DROP":
                count = 0
            if action == "HOLD":
                cap = max(cap, count)
            pieces.append(
                decision.model_copy(
                    update={
                        "source_start_us": start,
                        "source_end_us": end,
                        "action": action,
                        "output_frame_count": count,
                        "provenance": provenance,
                        "speed_ramp":None if override is not None else decision.speed_ramp,
                    }
                )
            )
            if fixed or action == "DROP":
                low = count
            elif musical:
                from sculptcut.music_episodes import episode_speed_limit

                low = min(cap, max(1, math.ceil(cap / episode_speed_limit(project)),
                                   math.ceil(cap / speed_ceiling) if speed_ceiling else 1,
                                   math.ceil(project.settings.beat_sync_min_shot_seconds * fps)))
            elif plan.settings.get("story") == "phrased_timelapse":
                limit = (project.settings.timelapse_bridge_max_speed
                         if provenance in ("story:process_bridge", "story:phrase_context")
                         else project.settings.timelapse_episode_max_speed)
                low = min(cap, max(1, math.ceil(cap/limit),
                                   math.ceil(cap/speed_ceiling) if speed_ceiling else 1))
            else:
                low = max(1, math.ceil(cap / 32),
                          math.ceil(cap / speed_ceiling) if speed_ceiling else 1)
            minimum.append(low)
            maximum.append(count if fixed or action == "DROP" else cap)
            preferred.append(float(max(1, count)))
    actual = max(sum(minimum), min(sum(maximum), plan.target_frame_count))
    counts = distribute(minimum, preferred, maximum, actual)
    updated = [
        d.model_copy(update={"output_frame_count": n}) for d, n in zip(pieces, counts, strict=True)
    ]
    if actual == 0:
        raise ValueError("Overrides drop all footage")
    result = plan.model_copy(
        update={
            "decisions": updated,
            "actual_frame_count": actual,
            "warnings": []
            if actual == plan.target_frame_count
            else ["Overrides make target infeasible"],
        }
    )
    validate_plan(project, result)
    return result


def exclude_dropped(project: Project, plan: RenderPlan) -> RenderPlan:
    """Remove shared exclusions before automatic candidate selection."""
    drops = [override for override in project.overrides if override.action == "DROP"]
    return apply_overrides(project.model_copy(update={"overrides": drops}), plan)
