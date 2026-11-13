"""Source-addressed manual edits can be reverted independently of chronological Undo."""
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from statistics import median

from sculptcut.domain import Override, Project
from sculptcut.planning import RenderDecision, RenderPlan
from sculptcut.source_selection import put_rule
from sculptcut.timewarp import output_before


@dataclass(frozen=True)
class ManualEdit:
    source_id: str
    start_us: int
    end_us: int
    action: str
    speed: float
    kind: str = 'changed'
    inherited: bool = True


JOURNAL_LIMIT = 200


def record_edits(before: Project, after: Project, plan: RenderPlan,
                 version: str, operation: str, previous: RenderPlan) -> RenderPlan:
    if before.overrides == after.overrides:
        return plan
    journal = json.loads(str(plan.settings.get('selection_edit_journal', '[]')))
    stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    for rule in after.overrides:
        if rule.exact_selection and not matches_baseline(rule, before.overrides):
            journal.append({'source_id': rule.source_id, 'start_us': rule.start_us,
                'end_us': rule.end_us, 'action': rule.action, 'created_at': stamp,
                'version': version, 'operation': operation})
    for edit in content_changes(previous, plan):
        journal.append({'source_id': edit.source_id, 'start_us': edit.start_us,
            'end_us': edit.end_us, 'action': edit.action, 'kind': edit.kind,
            'created_at': stamp, 'version': version, 'operation': operation})
    return plan.model_copy(update={'settings': plan.settings | {
        'selection_edit_journal': json.dumps(journal[-JOURNAL_LIMIT:])}})


def edit_origin(plan: RenderPlan, edit: ManualEdit) -> dict[str, str]:
    for event in reversed(json.loads(str(plan.settings.get('selection_edit_journal', '[]')))):
        if (event['source_id'] == edit.source_id and event['action'] == edit.action
                and event['start_us'] <= edit.start_us and edit.end_us <= event['end_us']):
            return {k: str(event[k]) for k in ('created_at', 'version', 'operation')}
    return {}


def content_changes(reference: RenderPlan, plan: RenderPlan) -> list[ManualEdit]:
    result = []
    for sid in {d.source_id for d in reference.decisions}:
        old = [d for d in reference.decisions if d.source_id == sid]
        new = [d for d in plan.decisions if d.source_id == sid]
        i = j = 0
        while i < len(old) and j < len(new):
            before, after = old[i], new[j]
            a = max(before.source_start_us, after.source_start_us)
            b = min(before.source_end_us, after.source_end_us)
            was, now = bool(before.output_frame_count), bool(after.output_frame_count)
            if a < b and was != now and before.action != 'HOLD' and after.action != 'HOLD':
                speed = (max(1.0, (after.source_end_us-after.source_start_us)*plan.output_fps
                             /(1e6*after.output_frame_count)) if now else 1.0)
                result.append(ManualEdit(sid, a, b, 'SET_SPEED' if now else 'DROP', speed,
                                         'added' if now else 'removed'))
            old_end, new_end = before.source_end_us, after.source_end_us
            if old_end <= new_end:
                i += 1
            if new_end <= old_end:
                j += 1
    return result


def baseline_rules(reference: RenderPlan) -> list[Override]:
    return [Override.model_validate(o) for o in json.loads(str(
        reference.settings.get('source_selection_baseline', '[]')))]


def reference_before_manual(project: Project, plan: RenderPlan) -> RenderPlan:
    """Recover exact legacy trims when a pre-edit layer was not saved yet.

    Only user-tagged pieces change; analytical omissions remain excluded.
    Legacy files have no old speed, so use the neighbouring work's median pace.
    """
    baseline = (baseline_rules(plan) if 'source_selection_baseline' in plan.settings
                else project.overrides)
    known = [o for o in baseline if o.exact_selection]
    if not known:
        return plan
    pace = median([d.output_frame_count/((d.source_end_us-d.source_start_us)*plan.output_fps/1e6)
                   for d in plan.decisions if d.action in ('SPEED', 'KEEP')
                   and d.output_frame_count] or [1.0])
    parts = []
    for d in plan.decisions:
        matching = [o for o in known if o.source_id == d.source_id
                    and o.start_us < d.source_end_us and d.source_start_us < o.end_us]
        if (not matching or d.action in ('HOLD', 'MONTAGE')
                or not d.provenance.startswith('user:')):
            parts.append(d)
            continue
        bounds = sorted({d.source_start_us, d.source_end_us} | {
            max(d.source_start_us, min(t, d.source_end_us))
            for o in matching for t in (o.start_us, o.end_us)})
        for a, b in zip(bounds, bounds[1:], strict=False):
            part = portion(d, a, b)
            if any(o.start_us <= a and b <= o.end_us for o in matching):
                natural = max(1, (b-a)*plan.output_fps//1_000_000)
                part = part.model_copy(update={'action': 'SPEED', 'output_frame_count': max(
                    1, min(natural, round(natural*pace))), 'speed_ramp': None,
                    'sample_times_us': [], 'provenance': 'story:manual_reference'})
            parts.append(part)
    count = sum(d.output_frame_count for d in parts)
    return plan.model_copy(update={'decisions': parts, 'actual_frame_count': count,
        'target_frame_count': count, 'settings': plan.settings | {'source_selection_baseline':
            json.dumps([o.model_dump(mode='json') for o in baseline if o not in known])}})


def manual_edits(project: Project, reference: RenderPlan, plan: RenderPlan) -> list[ManualEdit]:
    baseline = baseline_rules(reference)
    result: list[ManualEdit] = content_changes(reference, plan)
    for rule in project.overrides:
        if not rule.exact_selection or matches_baseline(rule, baseline):
            continue
        old = [d for d in reference.decisions if d.source_id == rule.source_id
               and d.source_start_us < rule.end_us and rule.start_us < d.source_end_us]
        new = [d for d in plan.decisions if d.source_id == rule.source_id
               and d.source_start_us < rule.end_us and rule.start_us < d.source_end_us]
        bounds = sorted({rule.start_us, rule.end_us} | {
            max(rule.start_us, min(t, rule.end_us)) for d in old+new
            for t in (d.source_start_us, d.source_end_us)})
        for a, b in zip(bounds, bounds[1:], strict=False):
            before = next((d for d in old if d.source_start_us <= a < d.source_end_us), None)
            after = next((d for d in new if d.source_start_us <= a < d.source_end_us), None)
            was = bool(before and before.output_frame_count)
            now = bool(after and after.output_frame_count)
            kind = ''
            edit = ManualEdit(rule.source_id, a, b, rule.action, rule.speed)
            if was and now and before is not None and after is not None:
                pace = ((before.source_end_us-before.source_start_us)*plan.output_fps
                        / (1e6*before.output_frame_count))
                origin = edit_origin(plan, edit)
                if (origin.get('operation') != 'bounds'
                        and (abs(pace-rule.speed) > 1e-6 or before.speed_ramp != after.speed_ramp)):
                    kind = 'changed'
            if kind:
                item = replace(edit, kind=kind)
                result.append(item)
    merged: list[ManualEdit] = []
    for item in sorted(result, key=lambda e: (e.source_id, e.start_us)):
        if (merged and merged[-1].source_id == item.source_id and merged[-1].end_us == item.start_us
                and merged[-1].kind == item.kind and merged[-1].action == item.action
                and abs(merged[-1].speed-item.speed) < 1e-6
                and edit_origin(plan, merged[-1]) == edit_origin(plan, item)):
            merged[-1] = replace(merged[-1], end_us=item.end_us)
        else:
            merged.append(item)
    return merged


def scoped_edits(project: Project, base: RenderPlan, reference: RenderPlan,
                 plan: RenderPlan, show_inherited: bool = False) -> list[ManualEdit]:
    current = [replace(e, inherited=False) for e in manual_edits(project, base, plan)]
    if not show_inherited:
        return current
    result = current[:]
    for edit in manual_edits(project, reference, plan):
        bounds = sorted({edit.start_us, edit.end_us} | {
            max(edit.start_us, min(t, edit.end_us)) for c in current
            if c.source_id == edit.source_id for t in (c.start_us, c.end_us)})
        for a, b in zip(bounds, bounds[1:], strict=False):
            if not any(c.source_id == edit.source_id and c.start_us <= a and b <= c.end_us
                       for c in current):
                result.append(replace(edit, start_us=a, end_us=b, inherited=True))
    return sorted(result, key=lambda e: (e.source_id, e.start_us, e.inherited))


def matches_baseline(rule: Override, baseline: list[Override]) -> bool:
    fields = {'start_us', 'end_us'}
    cursor = rule.start_us
    for old in sorted(baseline, key=lambda o: o.start_us):
        if (old.model_dump(exclude=fields) == rule.model_dump(exclude=fields)
                and old.start_us <= cursor < old.end_us):
            cursor = old.end_us
            if cursor >= rule.end_us:
                return True
    return False


def portion(d: RenderDecision, a: int, b: int) -> RenderDecision:
    if a == d.source_start_us and b == d.source_end_us:
        return d
    if d.action in ('HOLD', 'MONTAGE'):
        raise ValueError('Финальный кадр защищён. Выберите рабочий фрагмент.')
    count = output_before(d, b)-output_before(d, a) if d.output_frame_count else 0
    return d.model_copy(update={'source_start_us': a, 'source_end_us': b,
        'output_frame_count': count, 'action': d.action if count else 'DROP'})


def restore_range(project: Project, plan: RenderPlan, reference: RenderPlan,
                  edit: ManualEdit) -> tuple[Project, RenderPlan]:
    sid, a, b = edit.source_id, edit.start_us, edit.end_us
    original = [portion(d, max(a, d.source_start_us), min(b, d.source_end_us))
                for d in reference.decisions if d.source_id == sid
                and d.source_start_us < b and a < d.source_end_us]
    pieces = []
    inserted = False
    for d in plan.decisions:
        if d.source_id != sid or d.source_end_us <= a or d.source_start_us >= b:
            pieces.append(d)
            continue
        if d.source_start_us < a:
            pieces.append(portion(d, d.source_start_us, a))
        if not inserted:
            pieces.extend(original)
            inserted = True
        if d.source_end_us > b:
            pieces.append(portion(d, b, d.source_end_us))
    rules = []
    for rule in project.overrides:
        if rule.source_id != sid or rule.end_us <= a or rule.start_us >= b:
            rules.append(rule)
            continue
        if rule.start_us < a:
            rules.append(rule.model_copy(update={'end_us': a}))
        if rule.end_us > b:
            rules.append(rule.model_copy(update={'start_us': b}))
    project = project.model_copy(update={'overrides': rules})
    for rule in baseline_rules(reference):
        if rule.source_id == sid and rule.start_us < b and a < rule.end_us:
            project = put_rule(project, rule.model_copy(update={
                'start_us': max(a, rule.start_us), 'end_us': min(b, rule.end_us)}))
    count = sum(d.output_frame_count for d in pieces)
    return project, plan.model_copy(update={'decisions': pieces, 'actual_frame_count': count,
                                          'target_frame_count': count})
