"""Compose output trims and source-time episode edits against the selected export."""

import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event

from sculptcut.domain import Override, Project
from sculptcut.intro import body_start_frame, output_frame_count
from sculptcut.media import Cancelled
from sculptcut.output_cuts import cut_output, saved_soundtrack
from sculptcut.overrides import apply_overrides
from sculptcut.planning import RenderDecision, RenderPlan, validate_plan
from sculptcut.project import load_project, resolve_snapshot_sources
from sculptcut.rendering import render
from sculptcut.shorts_refill import (
    _expand_montage,
    _merged_cut_frames,
    _split_decision,
    cuts_on_plan_clock,
    refill_plan_after_cuts,
    refill_render_context,
)
from sculptcut.variants import read_variant, save_variant


@dataclass(frozen=True)
class CombinedEdit:
    snapshot: Project
    project: Project
    plan: RenderPlan
    base_plan: RenderPlan
    parent: Path
    cuts: list[tuple[int, int]]
    leading: list[tuple[int, int]]
    trailing: list[tuple[int, int]]

    def bookend_cuts(self) -> list[tuple[int, int]]:
        """Translate protected bookend offsets to the newly edited body's clock."""
        fps = self.plan.output_fps
        tail = body_start_frame(self.project.settings, fps, self.plan.actual_frame_count)
        tail += self.plan.actual_frame_count
        return [(start * 1_000_000 // fps, (end * 1_000_000 + fps - 1) // fps)
                for start, end in [*self.leading,
                                   *((tail + a, tail + b) for a, b in self.trailing)]]


def pending_episode_edits(root: Path, video: Path) -> bool:
    state = read_variant(video)
    if not state.get("project") or not state.get("plan") or not (root / "project.json").is_file():
        return False
    return load_project(root).overrides != Project.model_validate(state["project"]).overrides


def _neutral_plan(
    state: dict[str, object], project: Project, rendered: RenderPlan, trimmed: RenderPlan,
    body_cut: bool,
) -> RenderPlan:
    """Keep an episode-neutral layer so Undo can remove an edit after exporting."""
    if not state.get("episode_base_plan"):
        return trimmed
    base = RenderPlan.model_validate(state["episode_base_plan"])
    base = base.model_copy(update={"settings": base.settings | {
        "music_fingerprint": rendered.settings.get("music_fingerprint", ""),
        "intro_fingerprint": rendered.settings.get("intro_fingerprint", ""),
    }})
    exclusions: list[Override] = []
    for decision in trimmed.decisions:
        if decision.provenance != "user:shorts_manual_cut":
            continue
        if (exclusions and exclusions[-1].source_id == decision.source_id
                and exclusions[-1].end_us == decision.source_start_us):
            exclusions[-1] = exclusions[-1].model_copy(update={"end_us": decision.source_end_us})
        else:
            exclusions.append(Override(source_id=decision.source_id,
                                       start_us=decision.source_start_us,
                                       end_us=decision.source_end_us, action="DROP"))
    # A partial hold still points at the same image; preserve the removed
    # display fraction without inventing a different source timestamp.
    decisions = []
    for decision in base.decisions:
        if decision.action == "HOLD":
            old = next((d for d in rendered.decisions if d.action == "HOLD"
                        and d.source_id == decision.source_id
                        and d.source_start_us == decision.source_start_us), None)
            new = next((d for d in trimmed.decisions if d.action == "HOLD"
                        and d.source_id == decision.source_id
                        and d.source_start_us == decision.source_start_us), None)
            if old is not None and new is not None:
                decision = decision.model_copy(update={"output_frame_count": max(
                    1,
                    decision.output_frame_count * new.output_frame_count // old.output_frame_count,
                ), "provenance": new.provenance})
        decisions.append(decision)
    base = base.model_copy(update={
        "decisions": decisions, "actual_frame_count": sum(d.output_frame_count for d in decisions),
        "target_frame_count": trimmed.actual_frame_count if body_cut else base.target_frame_count,
    })
    result = apply_overrides(project.model_copy(update={"overrides": exclusions}),
                             base, manual=True)
    return result.model_copy(update={"decisions": [
        d.model_copy(update={"provenance": "user:shorts_manual_cut"})
        if d.action == "DROP" and any(o.source_id == d.source_id
                                     and o.start_us <= d.source_start_us
                                     and o.end_us >= d.source_end_us for o in exclusions) else d
        for d in result.decisions
    ], "settings": result.settings | {
        "manual_duration_policy": trimmed.settings.get("manual_duration_policy", "trim"),
    }})


def apply_episode_edits(project: Project, plan: RenderPlan) -> RenderPlan:
    result = apply_overrides(project, plan, manual=True)
    if plan.settings.get("manual_duration_policy") == "refill":
        result = refill_plan_after_cuts(project, result, [], allow_empty=True,
                                        respect_episode_speeds=True)
    return result


def prepare_combined_edit(
    root: Path, video: Path, cuts: list[tuple[int, int]] | None = None, *,
    apply_current: bool = True,
) -> CombinedEdit:
    state = read_variant(video)
    if not state.get("project") or not state.get("plan"):
        raise ValueError("У этой версии нет монтажного плана. Доступна обычная подрезка.")
    current = load_project(root)
    snapshot = resolve_snapshot_sources(Project.model_validate(state["project"]), current)
    snapshot = snapshot.model_copy(update={"overrides": current.overrides})
    original = RenderPlan.model_validate(state["plan"])
    project, original = refill_render_context(video, snapshot, original)
    validate_plan(project, original)
    mapped = cuts_on_plan_clock(video, project, original, cuts or [])
    fps = original.output_fps
    offset = body_start_frame(project.settings, fps, original.actual_frame_count)
    body_end = offset + original.actual_frame_count
    total = output_frame_count(project.settings, fps, original.actual_frame_count)
    leading, trailing = [], []
    for start_us, end_us in mapped:
        start, end = start_us * fps // 1_000_000, (end_us * fps + 999_999) // 1_000_000
        if start < offset:
            leading.append((start, min(end, offset)))
        if end > body_end:
            trailing.append((max(start, body_end) - body_end, min(end, total) - body_end))
    frame_cuts = _merged_cut_frames(mapped, fps, original.actual_frame_count, offset)
    decisions = []
    position = 0
    for part in (part for d in original.decisions for part in _expand_montage(d)):
        decisions.extend(_split_decision(part, position, frame_cuts))
        position += part.output_frame_count
    actual = sum(d.output_frame_count for d in decisions)
    if not actual:
        raise ValueError("Нельзя удалить весь ролик")
    plan = original.model_copy(update={
        "decisions": decisions, "actual_frame_count": actual,
        # Ordinary trimming remains shorter; a previously refilled plan already
        # contains its full budget. Episode overrides must not refill plain cuts.
        "target_frame_count": actual,
        "settings": {key: value for key, value in original.settings.items()
                     if key != "request_key"} | {
            "refill_target_frame_count": int(original.settings.get(
                "refill_target_frame_count", original.target_frame_count,
            )),
            "manual_duration_policy": "trim" if frame_cuts else original.settings.get(
                "manual_duration_policy", "trim",
            ),
        },
    })
    plan = _neutral_plan(state, project, original, plan, bool(frame_cuts))
    validate_plan(project, plan)
    base_plan = plan
    if apply_current:
        plan = apply_episode_edits(project, plan)
    plan = plan.model_copy(update={"settings": plan.settings | {
        "actual_output_frames": output_frame_count(project.settings, fps, plan.actual_frame_count),
    }})
    return CombinedEdit(snapshot, project, plan, base_plan, video, mapped, leading, trailing)


def prepare_refill_edit(root: Path, video: Path, cuts: list[tuple[int, int]]) -> CombinedEdit:
    """Use the same merged layers for the save dialog and the actual refill job."""
    edit = prepare_combined_edit(root, video, cuts, apply_current=False)
    plan = edit.plan.model_copy(update={"target_frame_count": int(
        edit.plan.settings.get("refill_target_frame_count", edit.plan.target_frame_count),
    )})
    base = refill_plan_after_cuts(edit.project, plan, [], allow_empty=True)
    base = base.model_copy(update={"settings": base.settings | {
        "manual_duration_policy": "refill",
    }})
    result = apply_episode_edits(edit.project, base)
    return replace(edit, plan=result, base_plan=base, leading=[], trailing=[])


def creation_edit(project: Project, plan: RenderPlan, previous: CombinedEdit) -> CombinedEdit:
    """Carry selected output exclusions onto a fresh plan in source coordinates.

    The new plan supplies the current preset, crop, music and duration. Output
    clock coordinates belong exclusively to the old version and cannot be
    applied to that new plan directly.
    """
    excluded = [d for d in previous.base_plan.decisions
                if d.provenance == "user:shorts_manual_cut"]
    partial = [d for d in previous.base_plan.decisions
               if d.action == "HOLD" and d.provenance == "user:shorts_partial_cut"]
    decisions: list[RenderDecision] = []
    for original in plan.decisions:
        for decision in _expand_montage(original):
            cuts = [d for d in excluded if d.source_id == decision.source_id
                    and d.source_start_us < decision.source_end_us
                    and d.source_end_us > decision.source_start_us]
            if decision.action == "HOLD":
                hold = next((d for d in partial if d.source_id == decision.source_id
                             and d.source_start_us == decision.source_start_us), None)
                if hold is not None:
                    decision = decision.model_copy(update={
                        "output_frame_count": min(decision.output_frame_count, max(
                            1, round(hold.output_frame_count * plan.output_fps
                                     / previous.base_plan.output_fps),
                        )), "provenance": "user:shorts_partial_cut",
                    })
            bounds = sorted({decision.source_start_us, decision.source_end_us} | {
                max(decision.source_start_us, min(t, decision.source_end_us))
                for d in cuts for t in (d.source_start_us, d.source_end_us)
            })
            duration = decision.source_end_us - decision.source_start_us
            for start, end in zip(bounds, bounds[1:], strict=False):
                removed = any(d.source_start_us <= start and end <= d.source_end_us for d in cuts)
                count = (decision.output_frame_count * (end - decision.source_start_us) // duration
                         - decision.output_frame_count * (start - decision.source_start_us)
                         // duration)
                if decision.action == "HOLD":
                    # A hold samples only its first timestamp. Keep that image
                    # and the exact exclusion mask for a later moving preset.
                    count = (decision.output_frame_count
                             if start == decision.source_start_us else 0)
                else:
                    count = min(count, max(1, (end - start) * plan.output_fps // 1_000_000))
                decisions.append(decision.model_copy(update={
                    "source_start_us": start, "source_end_us": end,
                    "output_frame_count": 0 if removed else count,
                    "action": "DROP" if removed or not count else decision.action,
                    "provenance": "user:shorts_manual_cut" if removed else decision.provenance,
                }))
    actual = sum(d.output_frame_count for d in decisions)
    if not actual:
        raise ValueError("Нельзя удалить весь ролик")
    policy = previous.base_plan.settings.get("manual_duration_policy", "trim")
    base = plan.model_copy(update={
        "decisions": decisions, "actual_frame_count": actual,
        "target_frame_count": plan.target_frame_count if policy == "refill" else actual,
        "settings": {key: value for key, value in plan.settings.items() if key != "request_key"}
        | {"manual_duration_policy": policy, "refill_target_frame_count": plan.target_frame_count},
    })
    validate_plan(project, base)
    result = apply_episode_edits(project, base)
    result = result.model_copy(update={"settings": result.settings | {
        "actual_output_frames": output_frame_count(
            project.settings, plan.output_fps, result.actual_frame_count,
        ),
    }})
    # Bookend masks are relative to their own block, never to the old body length.
    fps, old_fps = plan.output_fps, previous.plan.output_fps
    head = body_start_frame(project.settings, fps, result.actual_frame_count)
    tail = output_frame_count(project.settings, fps, result.actual_frame_count)
    tail -= head + result.actual_frame_count

    def scale(ranges: list[tuple[int, int]], limit: int) -> list[tuple[int, int]]:
        return [(max(0, a * fps // old_fps), min(limit, (b * fps + old_fps - 1) // old_fps))
                for a, b in ranges if a * fps // old_fps < limit]

    return CombinedEdit(project, project, result, base, previous.parent, [],
                        scale(previous.leading, head), scale(previous.trailing, tail))


def render_combined_edit(
    root: Path, edit: CombinedEdit, destination: Path, cancel: Event,
    progress: Callable[[float], None], *, encoder: str = "auto",
    encoder_status: Callable[[str], None] | None = None,
    preview: bool = False,
) -> Path:
    """Render body edits from originals, then retain any deliberately trimmed bookends."""
    if destination.suffix.lower() != ".mp4" or destination.exists():
        raise ValueError("Output must be a new .mp4 path")
    bookends = edit.bookend_cuts()
    if not bookends:
        return render(root, edit.project, edit.plan, destination, cancel, preview,
                      progress=progress,
                      encoder=encoder, encoder_status=encoder_status,
                      variant_project=edit.snapshot, variant_parent=edit.parent,
                      variant_operation="combined_edit", variant_cuts=edit.cuts,
                      variant_episode_base=edit.base_plan)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="combined-", dir=destination.parent) as scratch:
        base = Path(scratch) / "base.mp4"
        render(root, edit.project, edit.plan, base, cancel, preview, encoder=encoder,
               progress=lambda value: progress(value * 0.8), encoder_status=encoder_status,
               variant_project=edit.snapshot)
        result = cut_output(base, bookends, cancel, lambda value: progress(0.8 + value * 0.2))
        soundtrack = saved_soundtrack(base)
        if cancel.is_set():
            raise Cancelled("Сохранение отменено")
        os.link(result, destination)
        save_variant(destination, project=edit.snapshot, plan=edit.plan,
                     parent=edit.parent, operation="combined_edit", cuts=edit.cuts,
                     music=soundtrack[0] if soundtrack else None, bookend_cuts=bookends,
                     episode_base_plan=edit.base_plan)
    return destination
