"""Map alignment gaps in a saved Photoshop plan to its video and source clocks."""

from bisect import bisect_left
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from sculptcut.canvas_history import enabled as canvas_history_enabled
from sculptcut.domain import Project
from sculptcut.intro import body_start_frame, output_frame_count
from sculptcut.planning import RenderDecision, RenderPlan
from sculptcut.shorts_refill import saved_frame_ranges


@dataclass(frozen=True)
class AlignmentMarker:
    start: int
    end: int
    decision_frame: int
    decision: RenderDecision

    def original_time(self, frame: int) -> int:
        return source_timestamp(self.decision, self.decision_frame + frame - self.start)


def source_timestamp(decision: RenderDecision, frame: int) -> int:
    return decision.source_start_us + (
        (decision.source_end_us - decision.source_start_us) * frame // decision.output_frame_count
    )


def unmatched_ranges(decision: RenderDecision) -> Iterator[tuple[int, int]]:
    count = decision.output_frame_count
    camera = decision.subject_camera
    if (not count or decision.action not in ('KEEP', 'SPEED')
            or decision.speed_ramp is not None):
        return
    if camera is None or not (camera.canvas_registered or camera.frame_only):
        yield 0, count
        return
    times = [point.time_us for point in camera.points]
    first = None
    for frame in range(count):
        timestamp = source_timestamp(decision, frame)
        index = bisect_left(times, timestamp)
        nearest = min(range(max(0, index-1), min(len(times), index+1)),
                      key=lambda i: abs(times[i] - timestamp))
        point = camera.points[nearest]
        matched = (point.visible_region is not None if camera.frame_only
                   else point.reference_matched)
        if camera.closeup_group is not None:
            matched = matched and point.reference_matched
        if matched and first is not None:
            yield first, frame
            first = None
        elif not matched and first is None:
            first = frame
    if first is not None:
        yield first, count


def alignment_markers(
    video: Path, project: Project, plan: RenderPlan,
) -> tuple[int, list[AlignmentMarker]]:
    fps = plan.output_fps
    total = output_frame_count(project.settings, fps, plan.actual_frame_count)
    if not (canvas_history_enabled(project.settings) or plan.settings.get('photoshop_frame_only')):
        return total, []
    retained = saved_frame_ranges(video, project, plan)
    duration = sum(end-start for start, end in retained)
    # Older and manually framed versions may have no reference tracking at all.
    if not any(d.subject_camera and (d.subject_camera.canvas_registered
               or d.subject_camera.frame_only) for d in plan.decisions):
        return duration, []
    positions = []
    elapsed = 0
    for start, end in retained:
        positions.append((start, end, elapsed))
        elapsed += end-start
    markers = []
    cursor = body_start_frame(project.settings, fps, plan.actual_frame_count)
    for decision in plan.decisions:
        for start, end in unmatched_ranges(decision):
            for left, right, saved in positions:
                lo, hi = max(cursor+start, left), min(cursor+end, right)
                if lo < hi:
                    markers.append(AlignmentMarker(saved+lo-left, saved+hi-left,
                                                   lo-cursor, decision))
        cursor += decision.output_frame_count
    return duration, markers
