"""Keep a reviewed selection inside its full output budget, including bookend masks."""
import json

from sculptcut.budget import distribute
from sculptcut.domain import Project
from sculptcut.intro import body_start_frame, output_frame_count
from sculptcut.output_cuts import retained_ranges
from sculptcut.planning import RenderPlan


def removed_frames(cuts: list[tuple[int, int]], limit: int) -> int:
    removed, cursor = 0, 0
    for a, b in sorted(cuts):
        a, b = max(0, a), min(limit, b)
        if b > max(cursor, a):
            removed += b-max(cursor, a)
        cursor = max(cursor, b)
    return removed


def selection_frames(project: Project, plan: RenderPlan,
                     leading: list[tuple[int, int]], trailing: list[tuple[int, int]]) -> int:
    fps, count = plan.output_fps, plan.actual_frame_count
    head = body_start_frame(project.settings, fps, count)
    total = output_frame_count(project.settings, fps, count)
    leading = [tuple(p) for p in json.loads(str(plan.settings.get(
        'selection_head', json.dumps(leading))))]
    trailing = [tuple(p) for p in json.loads(str(plan.settings.get(
        'selection_tail', json.dumps(trailing))))]
    return total-removed_frames(leading, head)-removed_frames(trailing, total-head-count)


def output_ranges(project: Project, plan: RenderPlan, leading: list[tuple[int, int]],
                  trailing: list[tuple[int, int]]) -> list[tuple[int, int]]:
    head = body_start_frame(project.settings, plan.output_fps, plan.actual_frame_count)
    end = head+plan.actual_frame_count
    total = output_frame_count(project.settings, plan.output_fps, plan.actual_frame_count)
    cuts = [tuple(p) for p in json.loads(str(plan.settings.get(
        'selection_head', json.dumps(leading))))]
    cuts += [(end+a, end+b) for a, b in json.loads(str(plan.settings.get(
        'selection_tail', json.dumps(trailing))))]
    return retained_ranges(total, [(max(0, a), min(total, b)) for a, b in cuts
                                   if max(0, a) < min(total, b)])


def timeline_start(project: Project, plan: RenderPlan, leading: list[tuple[int, int]]) -> int:
    head = body_start_frame(project.settings, plan.output_fps, plan.actual_frame_count)
    if plan.settings.get('selection_duration_policy') == 'fixed':
        cuts = [tuple(p) for p in json.loads(str(plan.settings.get(
            'selection_head', json.dumps(leading))))]
        head -= removed_frames(cuts, head)
    return head


def timeline_frames(project: Project, plan: RenderPlan, leading: list[tuple[int, int]],
                    trailing: list[tuple[int, int]]) -> int:
    return (selection_frames(project, plan, leading, trailing)
            if plan.settings.get('selection_duration_policy') == 'fixed' else
            output_frame_count(project.settings, plan.output_fps, plan.actual_frame_count))


def fit_selection(project: Project, plan: RenderPlan, leading: list[tuple[int, int]],
                  trailing: list[tuple[int, int]]) -> RenderPlan:
    target = round(project.settings.target_seconds*plan.output_fps)
    active = [d for d in plan.decisions if d.output_frame_count]
    minimum = [d.output_frame_count if d.action in ('HOLD', 'MONTAGE') else 1 for d in active]
    maximum = [d.output_frame_count if d.action in ('HOLD', 'MONTAGE') else max(
        1, (d.source_end_us-d.source_start_us+1)*plan.output_fps//1_000_000) for d in active]
    # Very short bodies can overlap a bookend entirely. Solve on the actual
    # delivered clock rather than treating that crossfade as fixed overhead.
    low, high = sum(minimum), sum(maximum)
    while low < high:
        mid = (low+high)//2
        candidate = plan.model_copy(update={'actual_frame_count': mid})
        if selection_frames(project, candidate, leading, trailing) < target:
            low = mid+1
        else:
            high = mid
    body = low
    counts = iter(distribute(minimum, [float(d.output_frame_count) for d in active], maximum, body))
    decisions = [d.model_copy(update={'output_frame_count': next(counts)})
                 if d.output_frame_count else d for d in plan.decisions]
    return plan.model_copy(update={'decisions': decisions, 'actual_frame_count': body,
        'target_frame_count': body, 'settings': plan.settings | {
            'selection_duration_policy': 'fixed', 'selection_target_frames': target,
            'selection_head': plan.settings.get('selection_head', json.dumps(leading)),
            'selection_tail': plan.settings.get('selection_tail', json.dumps(trailing)),
            'actual_output_frames': output_frame_count(project.settings, plan.output_fps, body)}})
