"""Recognize deletion-only revisions that can retain the selected movie's pixels."""

from pathlib import Path

from sculptcut.bookends import final_frames
from sculptcut.canvas_history import enabled as canvas_history_enabled
from sculptcut.domain import Project
from sculptcut.intro import active_intro_image, body_start_frame
from sculptcut.planning import RenderPlan
from sculptcut.project import fingerprint
from sculptcut.variants import cut_history, read_variant


def episode_cut_ranges(video: Path, project: Project, plan: RenderPlan) -> list[tuple[int, int]]:
    state = read_variant(video)
    if state.get("video_fingerprint") and state["video_fingerprint"] != fingerprint(video):
        return []
    if (not state.get("plan") or not state.get("project") or state.get("bookend_cuts_us")
            or cut_history(video) or canvas_history_enabled(project.settings)
            or active_intro_image(project.settings)):
        return []
    old = RenderPlan.model_validate(state["plan"])
    tail = final_frames(old)
    settings = Project.model_validate(state["project"]).settings
    ignore = {"music_folder", "music_path", "output_name"}
    if (settings.model_dump(exclude=ignore) != project.settings.model_dump(exclude=ignore)
            or old.source_fingerprints != plan.source_fingerprints
            or old.output_fps != plan.output_fps or tail != final_frames(plan)
            or old.actual_frame_count <= plan.actual_frame_count
            or (old.settings.get("draft") and old.settings.get("preview_quality") != "original")):
        return []
    from sculptcut.rendering import export_size

    if export_size(project, old) != export_size(project, plan):
        return []
    wanted = iter(d for d in plan.decisions if d.output_frame_count)
    current = next(wanted, None)
    fps = old.output_fps
    offset = body_start_frame(settings, fps, old.actual_frame_count)
    position = end_kept = 0
    gaps = []
    timing = {"source_start_us", "source_end_us", "output_frame_count", "episode_id",
              "episode_score"}
    for original in old.decisions:
        count = original.output_frame_count
        if not count:
            continue
        length = original.source_end_us - original.source_start_us
        while (current and current.source_id == original.source_id
               and original.source_start_us <= current.source_start_us < original.source_end_us):
            if (current.source_end_us > original.source_end_us
                    or current.model_dump(exclude=timing) != original.model_dump(exclude=timing)):
                return []
            a = current.source_start_us - original.source_start_us
            b = current.source_end_us - original.source_start_us
            if a or b != length:
                if original.action not in ("KEEP", "SPEED") or original.speed_ramp:
                    return []
                # A split only preserves source sampling if both ends land on
                # the old output grid (source timestamps round to microseconds).
            left = (a * count + length // 2) // length
            right = (b * count + length // 2) // length
            if (abs(a - left * length // count) > 1 or abs(b - right * length // count) > 1
                    or right-left != current.output_frame_count or position+left < end_kept):
                return []
            if end_kept < position + left:
                gaps.append((end_kept, position + left))
            end_kept = position + right
            current = next(wanted, None)
        position += count
    if end_kept < position:
        gaps.append((end_kept, position))
    if current is not None or any(b > position - tail for _, b in gaps):
        return []
    return [(round((offset+a)*1e6/fps), round((offset+b)*1e6/fps)) for a, b in gaps]
