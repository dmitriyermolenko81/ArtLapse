"""Exact source-range editing against a saved montage, independent of Qt and analysis."""

import json
import math
from dataclasses import replace

from sculptcut.budget import distribute
from sculptcut.combined_edits import CombinedEdit
from sculptcut.domain import Override, Project
from sculptcut.intro import body_start_frame, output_frame_count
from sculptcut.planning import RenderPlan, validate_plan
from sculptcut.selection_duration import fit_selection, output_ranges, timeline_start
from sculptcut.shorts_refill import _expand_montage, _split_decision
from sculptcut.timewarp import output_before, source_time

VERSION = "source-selection-277"


def put_rule(project: Project, rule: Override) -> Project:
    rules = []
    for old in project.overrides:
        if (
            old.source_id != rule.source_id
            or old.end_us <= rule.start_us
            or old.start_us >= rule.end_us
        ):
            rules.append(old)
        else:
            if old.start_us < rule.start_us:
                rules.append(old.model_copy(update={"end_us": rule.start_us}))
            if old.end_us > rule.end_us:
                rules.append(old.model_copy(update={"start_us": rule.end_us}))
    return project.model_copy(update={"overrides": [*rules, rule]})


def merge_edit_rules(project: Project, before: list[Override], after: list[Override]) -> Project:
    """Carry this version's pending delta once, preserving unrelated project exclusions."""
    for sid in {r.source_id for r in before+after}:
        bounds = sorted({t for r in before+after if r.source_id == sid
                         for t in (r.start_us, r.end_us)})
        for a, b in zip(bounds, bounds[1:], strict=False):
            old = next((r for r in before if r.source_id == sid
                        and r.start_us <= a < r.end_us), None)
            new = next((r for r in after if r.source_id == sid
                        and r.start_us <= a < r.end_us), None)
            fields = {'start_us', 'end_us'}
            if ((old.model_dump(exclude=fields) if old else None)
                    == (new.model_dump(exclude=fields) if new else None)):
                continue
            if new is not None:
                project = put_rule(project, new.model_copy(update={'start_us': a, 'end_us': b}))
            else:
                rules = []
                for r in project.overrides:
                    if r.source_id != sid or r.end_us <= a or r.start_us >= b:
                        rules.append(r)
                    else:
                        if r.start_us < a:
                            rules.append(r.model_copy(update={'end_us': a}))
                        if r.end_us > b:
                            rules.append(r.model_copy(update={'start_us': b}))
                project = project.model_copy(update={'overrides': rules})
    return project


def change_range(plan: RenderPlan, rule: Override) -> RenderPlan:
    pieces = []
    for d in plan.decisions:
        if (
            d.source_id != rule.source_id
            or d.source_end_us <= rule.start_us
            or d.source_start_us >= rule.end_us
        ):
            pieces.append(d)
            continue
        if d.action in ("HOLD", "MONTAGE") or d.sample_times_us:
            raise ValueError("Финальный стоп-кадр сохраняется отдельно от рабочих сцен.")
        bounds = sorted(
            {
                d.source_start_us,
                d.source_end_us,
                max(d.source_start_us, rule.start_us),
                min(d.source_end_us, rule.end_us),
            }
        )
        for a, b in zip(bounds, bounds[1:], strict=False):
            hit = rule.start_us <= a and b <= rule.end_us
            count = (output_before(d, b) - output_before(d, a)) if d.output_frame_count else 0
            data: dict[str, object] = {
                "source_start_us": a,
                "source_end_us": b,
                "output_frame_count": count,
                "action": d.action if count else "DROP",
            }
            if hit:
                count = (
                    0
                    if rule.action == "DROP"
                    else max(1, math.floor((b - a) * plan.output_fps / 1_000_000 / rule.speed))
                )
                data.update(
                    output_frame_count=count,
                    action="SPEED" if count else "DROP",
                    speed_ramp=None,
                    sample_times_us=[],
                    provenance="user:source_selection",
                    episode_id=None,
                    episode_score=None,
                )
            part = d.model_copy(update=data)
            previous = pieces[-1] if pieces else None
            if (
                hit
                and previous is not None
                and previous.provenance == "user:source_selection"
                and previous.source_id == part.source_id
                and previous.source_end_us == a
                and previous.source_start_us >= rule.start_us
                and previous.crop == part.crop
                and previous.subject_camera == part.subject_camera
            ):
                frames = (
                    0
                    if rule.action == "DROP"
                    else max(
                        1,
                        math.floor(
                            (b - previous.source_start_us)
                            * plan.output_fps
                            / 1_000_000
                            / rule.speed
                        ),
                    )
                )
                pieces[-1] = previous.model_copy(
                    update={"source_end_us": b, "output_frame_count": frames}
                )
            else:
                pieces.append(part)
    count = sum(d.output_frame_count for d in pieces)
    if not count:
        raise ValueError("Оставьте хотя бы один участок в монтаже.")
    return plan.model_copy(
        update={
            "decisions": pieces,
            "actual_frame_count": count,
            "target_frame_count": count,
            "warnings": [],
        }
    )


def apply_selection_rules(project: Project, base: RenderPlan) -> RenderPlan:
    baseline = [
        Override.model_validate(o)
        for o in json.loads(str(base.settings.get("source_selection_baseline", "[]")))
    ]
    result = base
    for rule in project.overrides:
        if rule not in baseline:
            result = change_range(result, rule)
    if base.settings.get('selection_duration_policy') == 'fixed':
        result = fit_selection(project, result, [], [])
    validate_plan(project, result)
    return result


class SourceSelection:
    def __init__(self, edit: CombinedEdit, *, fixed_duration: bool = False) -> None:
        from sculptcut.shorts_layout import SELECTION_PRESETS

        if edit.project.settings.preset not in SELECTION_PRESETS:
            raise ValueError("Редактирование отбора доступно для таймлапса и видеоэпизодов Shorts.")
        self.original = edit
        settings = {k: v for k, v in edit.plan.settings.items() if k != "request_key"} | {
            "source_selection": VERSION,
            "source_selection_baseline": json.dumps(
                [o.model_dump(mode="json") for o in edit.project.overrides]
            ),
        }
        self.base = edit.plan.model_copy(update={"settings": settings})
        self.plan = self.base
        from sculptcut.selection_history import reference_before_manual

        self.reference = reference_before_manual(edit.project, self.base)
        self.fixed_duration = fixed_duration or settings.get('selection_duration_policy') == 'fixed'
        self.project = edit.project
        self.undo_stack: list[tuple[RenderPlan, Project]] = []
        self.redo_stack: list[tuple[RenderPlan, Project]] = []
        self.target = edit.plan.actual_frame_count
        self.restored_cursor: int | None = None
        self.restored_view: tuple[float, float] | None = None
        self.restore_note = ""

    @property
    def changed(self) -> bool:
        return self.plan != self.base

    def trim_movie(self, start_ms: int, end_ms: int) -> None:
        plan, project = self.plan, self.project
        fps = plan.output_fps
        total = output_frame_count(project.settings, fps, plan.actual_frame_count)
        offset = body_start_frame(project.settings, fps, plan.actual_frame_count)
        a, b = round(start_ms * fps / 1000), round(end_ms * fps / 1000)
        if plan.settings.get('selection_duration_policy') == 'fixed':
            from sculptcut.output_cuts import source_time

            ranges = output_ranges(project, plan, self.original.leading, self.original.trailing)
            a, b = source_time(ranges, a), source_time(ranges, b)
        if not 0 <= a < b <= total:
            raise ValueError("Оставьте часть ролика между границами.")
        cuts = [(0, max(0, a-offset)), (max(0, b-offset), plan.actual_frame_count)]
        cuts = [(x, y) for x, y in cuts if x < y]
        parts = []
        clock = 0
        for d in (part for decision in plan.decisions for part in _expand_montage(decision)):
            split = _split_decision(d, clock, cuts)
            parts.extend(split)
            if d.output_frame_count:
                for part in split:
                    if not part.output_frame_count:
                        project = put_rule(project, self.range_rule(
                            part.source_id, part.source_start_us, part.source_end_us, None))
            clock += d.output_frame_count
        count = sum(d.output_frame_count for d in parts)
        if not count:
            raise ValueError("Оставьте хотя бы один эпизод.")
        head = json.loads(str(plan.settings.get(
            "selection_head", json.dumps(self.original.leading))))
        tail = json.loads(str(plan.settings.get(
            "selection_tail", json.dumps(self.original.trailing))))
        if a > 0 and offset > 0:
            head.append([0, min(a, offset)])
        body_end = offset + plan.actual_frame_count
        if b < total and total > body_end:
            tail.append([max(0, b-body_end), total-body_end])
        self.save(plan.model_copy(update={"decisions": parts,
            "actual_frame_count": count, "target_frame_count": count,
            "settings": plan.settings | {"selection_head": json.dumps(head),
                                          "selection_tail": json.dumps(tail)}}), project)

    def source_bounds(self, source_id: str, start: int, end: int) -> None:
        source = next(s for s in self.project.sources if s.id == source_id)
        if not 0 <= start < end <= source.duration_us:
            raise ValueError("Выберите участок внутри исходника: начало должно быть раньше конца.")

    def save(self, plan: RenderPlan, project: Project, *, operation: str = 'range') -> None:
        from sculptcut.selection_history import record_edits

        plan = record_edits(self.project, project, plan,
                            self.original.parent.name, operation, self.plan)
        if self.fixed_duration:
            plan = fit_selection(project, plan, self.original.leading, self.original.trailing)
        validate_plan(project, plan)
        self.undo_stack.append((self.plan, self.project))
        self.redo_stack.clear()
        self.plan, self.project = plan, project

    def restore_manual(self, edit: object, start_us: int | None = None,
                       end_us: int | None = None) -> None:
        from sculptcut.selection_history import ManualEdit, restore_range

        if not isinstance(edit, ManualEdit):
            raise ValueError('Не выбрана ручная правка.')
        reference = self.reference if edit.inherited else self.base
        project, plan = restore_range(self.project, self.plan, reference, edit)
        if start_us is not None and end_us is not None:
            rule = self.range_rule(edit.source_id, start_us, end_us,
                                   None if edit.action == 'DROP' else edit.speed)
            plan, project = change_range(plan, rule), put_rule(project, rule)
        self.save(plan, project, operation='revise' if start_us is not None else 'restore')

    def range_rule(self, source_id: str, start: int, end: int, speed: float | None) -> Override:
        self.source_bounds(source_id, start, end)
        return Override(
            source_id=source_id,
            start_us=start,
            end_us=end,
            action="DROP" if speed is None else "SET_SPEED",
            speed=speed or 1,
            restore_omitted=speed is not None,
            exact_selection=True,
        )

    def select(self, source_id: str, start: int, end: int, speed: float | None) -> None:
        rule = self.range_rule(source_id, start, end, speed)
        self.save(change_range(self.plan, rule), put_rule(self.project, rule))

    def split_points(self) -> set[tuple[str, int]]:
        """Source anchors survive retiming, draft reload and reviewed exports."""
        stored = self.plan.settings.get('selection_split_points')
        if stored is not None:
            return {(str(sid), int(stamp)) for sid, stamp in json.loads(str(stored))} | {
                (sid, t) for sid, points in self.project.source_splits.items() for t in points}
        # Stage314 drafts stored the actual pieces, before explicit split markers.
        # Only recover a seam inside one unchanged original work decision.
        points = {(sid, t) for sid, times in self.project.source_splits.items() for t in times}
        for left, right in zip(self.plan.decisions, self.plan.decisions[1:], strict=False):
            if (not left.output_frame_count or not right.output_frame_count
                    or left.action not in ('KEEP', 'SPEED') or left.action != right.action
                    or left.source_id != right.source_id
                    or left.source_end_us != right.source_start_us):
                continue
            if any(old.source_id == left.source_id
                   and old.source_start_us <= left.source_start_us < left.source_end_us
                   < right.source_end_us <= old.source_end_us
                   and old.action == left.action
                   and old.provenance == left.provenance == right.provenance
                   and old.episode_id == left.episode_id == right.episode_id
                   and old.speed_ramp == left.speed_ramp == right.speed_ramp
                   and old.crop == left.crop == right.crop
                   and old.subject_camera == left.subject_camera == right.subject_camera
                   for old in self.original.base_plan.decisions):
                points.add((left.source_id, left.source_end_us))
        return points

    def split_at(self, position_ms: int) -> tuple[str, int] | None:
        """Split work at an output frame, preserving coverage, timing and ramp phase."""
        plan = self.plan
        frame = round(position_ms * plan.output_fps / 1000)
        clock = timeline_start(self.project, plan, self.original.leading)
        for index, decision in enumerate(plan.decisions):
            count = decision.output_frame_count
            if clock < frame < clock + count:
                if decision.action not in ('KEEP', 'SPEED') or decision.sample_times_us:
                    return None
                left = frame - clock
                boundary = (source_time(decision, left / count) if decision.speed_ramp else
                    decision.source_start_us + (
                        (decision.source_end_us-decision.source_start_us)*left+count//2)//count)
                if not decision.source_start_us < boundary < decision.source_end_us:
                    return None
                parts = [decision.model_copy(update={
                    'source_end_us': boundary, 'output_frame_count': left}),
                    decision.model_copy(update={
                        'source_start_us': boundary, 'output_frame_count': count-left})]
                points = self.split_points() | {(decision.source_id, boundary)}
                updated = plan.model_copy(update={'decisions': [
                    *plan.decisions[:index], *parts, *plan.decisions[index+1:]],
                    'settings': plan.settings | {
                        'selection_split_points': json.dumps(sorted(points))}})
                shared = {sid: list(times) for sid, times in self.project.source_splits.items()}
                shared[decision.source_id] = sorted(set(shared.get(decision.source_id, []))
                                                    | {boundary})
                self.save(updated, self.project.model_copy(update={'source_splits': shared}),
                          operation='split')
                return decision.source_id, boundary
            clock += count
        return None  # Bookends, existing seams and one-frame scenes are unchanged.

    def resize(self, index: int, start: int, end: int, speed: float) -> None:
        d = self.plan.decisions[index]
        self.source_bounds(d.source_id, start, end)
        if d.action in ("DROP", "HOLD", "MONTAGE"):
            raise ValueError("Выберите рабочую сцену в верхней дорожке.")
        for other in self.plan.decisions:
            if (
                other is not d
                and other.source_id == d.source_id
                and other.output_frame_count
                and other.source_start_us < end
                and start < other.source_end_us
            ):
                raise ValueError("Граница дошла до соседней сцены. Выберите меньший участок.")
        plan, project = self.plan, self.project
        if start > d.source_start_us:
            rule = self.range_rule(
                d.source_id, d.source_start_us, min(start, d.source_end_us), None
            )
            plan, project = change_range(plan, rule), put_rule(project, rule)
        if end < d.source_end_us:
            rule = self.range_rule(d.source_id, max(end, d.source_start_us), d.source_end_us, None)
            plan, project = change_range(plan, rule), put_rule(project, rule)
        rule = self.range_rule(d.source_id, start, end, speed)
        self.save(change_range(plan, rule), put_rule(project, rule), operation='bounds')

    def undo(self, redo: bool = False) -> None:
        source, target = (
            (self.redo_stack, self.undo_stack) if redo else (self.undo_stack, self.redo_stack)
        )
        if source:
            target.append((self.plan, self.project))
            self.plan, self.project = source.pop()
            if self.fixed_duration and self.changed:
                self.plan = fit_selection(self.project, self.plan,
                                          self.original.leading, self.original.trailing)

    def fit_duration(self, maximum_speed: float = 32) -> None:
        if self.fixed_duration:
            fitted = fit_selection(self.project, self.plan,
                                   self.original.leading, self.original.trailing)
            if fitted != self.plan:
                self.save(fitted, self.project)
            return
        active = [d for d in self.plan.decisions if d.output_frame_count]
        lows = [
            d.output_frame_count
            if d.action in ("HOLD", "MONTAGE")
            else max(
                1,
                math.ceil(
                    (d.source_end_us - d.source_start_us)
                    * self.plan.output_fps
                    / 1_000_000
                    / maximum_speed
                ),
            )
            for d in active
        ]
        caps = [
            d.output_frame_count
            if d.action in ("HOLD", "MONTAGE")
            else max(1, (d.source_end_us - d.source_start_us) * self.plan.output_fps // 1_000_000)
            for d in active
        ]
        target = max(sum(lows), min(sum(caps), self.target))
        counts = iter(distribute(lows, [float(d.output_frame_count) for d in active], caps, target))
        project = self.project
        decisions = []
        for d in self.plan.decisions:
            if not d.output_frame_count:
                decisions.append(d)
                continue
            count = next(counts)
            if d.action not in ("HOLD", "MONTAGE"):
                speed = max(
                    1.0, (d.source_end_us - d.source_start_us) * self.plan.output_fps / 1e6 / count
                )
                project = put_rule(
                    project, self.range_rule(d.source_id, d.source_start_us, d.source_end_us, speed)
                )
                d = d.model_copy(update={"speed_ramp": None})
            decisions.append(d.model_copy(update={"output_frame_count": count}))
        self.save(
            self.plan.model_copy(
                update={
                    "decisions": decisions,
                    "actual_frame_count": target,
                    "target_frame_count": target,
                }
            ),
            project,
        )

    def export_edit(self) -> CombinedEdit:
        snapshot = self.original.snapshot.model_copy(update={"overrides": self.project.overrides,
            'source_splits': self.project.source_splits})
        policy = {k: v for k, v in self.plan.settings.items() if k in (
            'selection_duration_policy', 'selection_target_frames',
            'selection_head', 'selection_tail')}
        return replace(
            self.original,
            snapshot=snapshot,
            project=self.project,
            plan=self.plan,
            base_plan=self.reference.model_copy(update={
                'settings': self.reference.settings | policy}),
            leading=[tuple(p) for p in json.loads(str(self.plan.settings.get(
                "selection_head", json.dumps(self.original.leading))))],
            trailing=[tuple(p) for p in json.loads(str(self.plan.settings.get(
                "selection_tail", json.dumps(self.original.trailing))))],
        )
