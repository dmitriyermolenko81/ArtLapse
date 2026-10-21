"""Shared final presentation for timelapses and every Shorts layout."""

import math

from sculptcut.budget import distribute
from sculptcut.domain import Project
from sculptcut.planning import RenderPlan, validate_plan

FINAL_ZOOM = 0.06
FINAL_PROVENANCES = ("user:shorts_final", "user:final_tail")


def present_final_result(project: Project, plan: RenderPlan) -> RenderPlan:
    """Reserve a readable finale without resurrecting removed source footage.

    Beat stills already have a deliberately timed final hold. Keep their musical
    boundaries; enable the same gentle zoom on that existing hold.
    """
    if project.settings.preset == "speed" or project.settings.final_view_seconds <= 0:
        return plan
    kept = [i for i, d in enumerate(plan.decisions) if d.action != "DROP"]
    if not kept:
        return plan
    index = kept[-1]
    last = plan.decisions[index]
    if last.provenance in ("user:KEEP", "user:SET_SPEED", "user:LOCK_SEGMENT"):
        return plan
    decisions = list(plan.decisions)
    beat = plan.settings.get("story") in ("beat_stills", "photoshop_beat_stills")
    if beat:
        return plan.model_copy(update={"settings": plan.settings | {
            "bookends": True, "final_presentation_frames": last.output_frame_count,
            "final_view_frames": last.output_frame_count,
        }})
    desired = min(round(min(4, project.settings.final_view_seconds) * plan.output_fps),
                  plan.actual_frame_count // 5)
    existing = 0
    for i in reversed(kept):
        if plan.decisions[i].provenance not in FINAL_PROVENANCES:
            break
        existing += plan.decisions[i].output_frame_count
    if existing >= desired and existing > 0:
        return plan.model_copy(update={"settings": plan.settings | {
            "bookends": True, "final_view_frames": existing,
        }})
    if not existing and last.action != "HOLD" and last.output_frame_count > desired > 0:
        # A plain/weighted timelapse may have no pre-labelled finale. Split its
        # final clip rather than freezing that entire (potentially long) clip.
        cut = last.source_end_us - max(1, round(
            (last.source_end_us-last.source_start_us)*desired/last.output_frame_count))
        if last.source_start_us < cut:
            body = last.model_copy(update={
                "source_end_us": cut, "output_frame_count": last.output_frame_count-desired,
            })
            tail = last.model_copy(update={
                "source_start_us": cut, "action": "HOLD", "output_frame_count": desired,
                "provenance": "user:final_tail",
            })
            if tail.subject_camera:
                from sculptcut.subject_camera import center_at, size_at

                camera = tail.subject_camera
                x, y = center_at(camera, tail.source_end_us)
                width, height = size_at(camera, tail.source_end_us)
                tail = tail.model_copy(update={"subject_camera": camera.model_copy(update={
                    "points": [camera.points[0].model_copy(update={
                        "time_us": 0, "x": x, "y": y, "width": width, "height": height,
                    })],
                })})
            decisions[index:index+1] = [body, tail]
            result = plan.model_copy(update={
                "decisions": decisions,
                "settings": plan.settings | {"bookends": True, "final_view_frames": desired},
            })
            validate_plan(project, result)
            return result
    desired = (last.output_frame_count + max(0, desired-existing)
               if existing else max(last.output_frame_count, desired))
    minimum, maximum, weights = [], [], []
    for d in decisions:
        fixed = d.provenance.startswith("user:") and d.provenance not in (
            "user:process_story", "user:shorts_highlight",
        )
        fixed = fixed or d.provenance in ("story:process_bridge", "story:setup_context")
        low = d.output_frame_count if fixed else int(d.action != "DROP")
        if (project.settings.preset == "timelapse" and d.action != "DROP"
                and not fixed and d.action != "HOLD"):
            low = max(low, math.ceil((d.source_end_us-d.source_start_us)*plan.output_fps
                                    / (1_000_000*project.settings.timelapse_episode_max_speed)))
        minimum.append(low)
        maximum.append(d.output_frame_count if fixed or d.action == "DROP"
                       else max(d.output_frame_count,
                                (d.source_end_us-d.source_start_us)*plan.output_fps//1_000_000))
        weights.append(float(max(1, d.output_frame_count)))
    available = plan.actual_frame_count - sum(minimum[i] for i in kept if i != index)
    count = min(desired, available)
    if count <= 0:
        return plan
    minimum[index] = maximum[index] = count
    update: dict[str, object] = {"provenance": last.provenance}
    if last.provenance not in FINAL_PROVENANCES:
        update["provenance"] = "user:shorts_final"
    if count > last.output_frame_count:
        update["action"] = "HOLD"
        if last.subject_camera:
            from sculptcut.subject_camera import center_at, size_at

            camera = last.subject_camera
            x, y = center_at(camera, last.source_end_us)
            width, height = size_at(camera, last.source_end_us)
            update["subject_camera"] = camera.model_copy(update={
                "points": [camera.points[0].model_copy(update={
                    "time_us": 0, "x": x, "y": y, "width": width, "height": height,
                })],
            })
    decisions[index] = last.model_copy(update=update)
    counts = distribute(minimum, weights, maximum, plan.actual_frame_count)
    result = plan.model_copy(update={
        "decisions": [d.model_copy(update={"output_frame_count": n})
                      for d, n in zip(decisions, counts, strict=True)],
        "settings": plan.settings | {
            "bookends": True,
            "final_view_frames": count + max(0, existing-last.output_frame_count),
        },
    })
    validate_plan(project, result)
    return result


def final_frames(plan: RenderPlan) -> int:
    if not plan.settings.get("bookends"):
        return 0
    explicit = int(plan.settings.get("final_presentation_frames", 0))
    if 0 < explicit <= plan.actual_frame_count:
        return explicit
    count = 0
    for decision in reversed(plan.decisions):
        if not decision.output_frame_count:
            continue
        if decision.provenance not in FINAL_PROVENANCES:
            break
        count += decision.output_frame_count
    return count if count >= plan.output_fps // 2 else 0


def bookend_filter(total: int, tail: int, fps: int, width: int, height: int) -> str:
    body_end = total - tail
    zoom = (
        f"zoompan=z='1+{FINAL_ZOOM}*on/{max(1, tail - 1)}':"
        f"x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s={width}x{height}:fps={fps}"
    )
    if body_end <= 0:
        return zoom
    return (
        f"split=2[work][final];"
        f"[work]trim=end_frame={body_end},settb=1/{fps},setpts=N[w];"
        f"[final]trim=start_frame={body_end}:end_frame={total},"
        f"settb=1/{fps},setpts=PTS-STARTPTS,{zoom},settb=1/{fps},setpts=N[f];"
        f"[w][f]concat=n=2:v=1:a=0,settb=1/{fps},setpts=N"
    )
