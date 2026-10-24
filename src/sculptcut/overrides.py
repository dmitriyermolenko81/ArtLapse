"""Non-destructive user decisions applied only to the render plan."""

import math

from sculptcut.budget import distribute
from sculptcut.domain import Project
from sculptcut.planning import RenderDecision, RenderPlan, validate_plan


def apply_overrides(project: Project, plan: RenderPlan, *, manual: bool = False) -> RenderPlan:
    if not project.overrides or (project.settings.preset == "speed" and not manual):
        return plan
    stills = plan.settings.get("story") in ("beat_stills", "photoshop_beat_stills")
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
            # Still frames have no playback speed; keep their musical duration.
            and (not stills or o.action == "DROP")
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
            count = max(
                1,
                round(
                    decision.output_frame_count
                    * (end - start)
                    / (decision.source_end_us - decision.source_start_us)
                ),
            )
            cap = max(1, math.floor((end - start) * fps / 1e6))
            action = decision.action
            fixed = decision.provenance == "user:final_tail" or decision.action == "HOLD"
            provenance = decision.provenance
            if override:
                provenance = f"user:{override.action}"
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
                    }
                )
            )
            minimum.append(count if fixed or action == "DROP" else max(1, math.ceil(cap / 32)))
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
