"""Chronological scene alternatives on an immutable output clock (timelapse only)."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from sculptcut.domain import Project, Roi
from sculptcut.planning import RenderDecision, RenderPlan, validate_plan
from sculptcut.project import load_project
from sculptcut.work_episodes import WorkEpisode

VERSION = "episode-roll-267"
# Older exports marked never-inspected candidates as unverified. A later,
# matching positive catalogue may admit their verified window; user/quality
# exclusions remain forbidden. Mere absence of evidence never admits footage.
EDITORIAL_DROPS = {"story:work_candidate_omission", "story:roll_omission", "story:unverified_work"}


def catalogue_snapshot(root: Path, project: Project, plan: RenderPlan) -> dict[str, Any] | None:
    """Archive eligible alternatives with the export, including legacy catalogues."""
    path = root / "work_episodes.json"
    if project.settings.preset != "timelapse" or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # An optional derived catalogue must not invalidate an export.
    if not isinstance(data, dict) or not isinstance(data.get("candidates"), list):
        return None
    fingerprints = {s.id: s.fingerprint for s in project.sources}
    rois = {sid: roi.model_dump() for sid, roi in project.rois.items()}
    if "source_fingerprints" not in data:
        # Earlier 0.1.3 catalogues have no identity. Only use the matching local
        # project; never borrow candidates from another export's sources/ROI.
        try:
            current = load_project(root)
        except (OSError, ValueError):
            return None
        data = data | {"source_fingerprints": {s.id: s.fingerprint for s in current.sources},
                       "rois": {sid: roi.model_dump() for sid, roi in current.rois.items()}}
    if data["source_fingerprints"] != fingerprints or data.get("rois", {}) != rois:
        return None
    if plan.source_fingerprints != fingerprints:
        return None
    return {"source_fingerprints": fingerprints, "rois": rois,
            "complete": all(e.get("process_checked") for e in data["candidates"]
                            if isinstance(e, dict)),
            "candidates": [e for e in data.get("candidates", [])
                           if isinstance(e, dict) and e.get("process_checked")
                           and e.get("process_verified")]}


def catalogue_episodes(data: dict[str, Any] | None, project: Project) -> list[WorkEpisode]:
    if not data or data.get("source_fingerprints") != {
            s.id: s.fingerprint for s in project.sources}:
        return []
    if data.get("rois", {}) != {sid: roi.model_dump() for sid, roi in project.rois.items()}:
        return []
    return [WorkEpisode.model_validate({k: v for k, v in e.items() if k not in ("id", "selected")})
            for e in data.get("candidates", []) if e.get("process_checked")
            and e.get("process_verified")]


@dataclass(frozen=True)
class RollSlot:
    index: int
    output_start: int
    original: RenderDecision


@dataclass(frozen=True)
class ChoiceSettings:
    maximum_alternatives: int = 4
    relative_minimum_change: float = 0.6
    minimum_peak_spacing_us: int = 3_000_000
    minimum_change: float = 0.04


CHOICES = ChoiceSettings()


class EpisodeRoll:
    def __init__(self, project: Project, plan: RenderPlan, catalogue: list[WorkEpisode]) -> None:
        if project.settings.preset != "timelapse":
            raise ValueError("Замена эпизодов пока доступна только в таймлапсе.")
        validate_plan(project, plan)
        self.project, self.base = project, plan
        self.offsets = {s.id: sum(p.duration_us for p in project.sources[:i])
                        for i, s in enumerate(project.sources)}
        self.lengths = {s.id: s.duration_us for s in project.sources}
        self.catalogue = catalogue
        self.slots = []
        position = 0
        for index, d in enumerate(plan.decisions):
            if d.output_frame_count:
                self.slots.append(RollSlot(index, position, d))
                position += d.output_frame_count
        self.choices = [s.original for s in self.slots]
        self.undo_stack: list[list[RenderDecision]] = []
        self.redo_stack: list[list[RenderDecision]] = []

    def start(self, d: RenderDecision) -> int:
        return self.offsets[d.source_id] + d.source_start_us

    def end(self, d: RenderDecision) -> int:
        return self.offsets[d.source_id] + d.source_end_us

    @property
    def changed(self) -> bool:
        return self.choices != [s.original for s in self.slots]

    def options(self, row: int) -> list[RenderDecision]:
        slot, current = self.slots[row], self.choices[row]
        if slot.original.action not in ("SPEED", "KEEP"):
            return [current]  # Deliberate final stills retain their image/duration.
        left = self.end(self.choices[row - 1]) if row else 0
        right = (self.start(self.choices[row + 1]) if row + 1 < len(self.slots)
                 else sum(self.lengths.values()))
        forbidden = [(d.source_id, d.source_start_us, d.source_end_us)
                     for d in self.base.decisions if d.action == "DROP"
                     and d.provenance not in EDITORIAL_DROPS]
        forbidden += [(o.source_id, o.start_us, o.end_us) for o in self.project.overrides
                      if o.action == "DROP"]
        result = [current]
        original = slot.original
        if left <= self.start(original) and self.end(original) <= right and original != current:
            result.append(original)
        for e in self.catalogue:
            a = e.work_start_us if e.work_start_us is not None else e.start_us
            b = e.work_end_us if e.work_end_us is not None else e.end_us
            if (not e.process_checked or not e.process_verified or e.source_id not in self.offsets
                    or not 0 <= a < b <= self.lengths[e.source_id]
                    or (b-a) * self.base.output_fps // 1_000_000 < original.output_frame_count
                    or self.offsets[e.source_id] + a < left
                    or self.offsets[e.source_id] + b > right
                    or any(sid == e.source_id and x < b and y > a for sid, x, y in forbidden)):
                continue
            if any(d.source_id == e.source_id and d.source_start_us == a
                   and d.source_end_us == b for d in result):
                continue
            # A candidate's camera/ramp belongs to its new source window. Never
            # carry a tracked crop from the scene being replaced to another one.
            crop: Roi | Literal["full"] = (self.project.rois.get(e.source_id, "full")
                    if original.crop != "full" else "full")
            result.append(RenderDecision(source_id=e.source_id, source_start_us=a,
                source_end_us=b, action="SPEED", output_frame_count=original.output_frame_count,
                crop=crop, provenance="user:episode_roll", episode_id=e.id, episode_score=e.score))
        protected = [d for d in result if d == current or d == original]
        by_id = {e.id:e for e in self.catalogue}

        def strength(d: RenderDecision) -> float:
            e = by_id.get(d.episode_id or "")
            # Legacy verified catalogues without window evidence retain their
            # established score; UNKNOWN/unverified windows never reach this pool.
            return (e.process_strength if e and e.process_strength is not None
                    else d.episode_score or 0)

        candidates = sorted((d for d in result if d not in protected
                             and strength(d) >= CHOICES.minimum_change),
                            key=lambda d:(-strength(d), -(d.episode_score or 0),
                                -abs(self.start(d)-self.start(original)), self.start(d)))
        best = max([strength(d) for d in protected]
                   + ([strength(candidates[0])] if candidates else [0]))
        chosen: list[RenderDecision] = []
        for d in candidates:
            if strength(d) < best * CHOICES.relative_minimum_change:
                break
            center = (self.start(d)+self.end(d))//2
            if any(abs(center-(self.start(p)+self.end(p))//2)
                   < CHOICES.minimum_peak_spacing_us for p in chosen):
                continue
            chosen.append(d)
            if len(chosen) == CHOICES.maximum_alternatives:
                break
        return sorted([*protected, *chosen], key=self.start)

    def select(self, row: int, candidate: RenderDecision) -> None:
        if candidate not in self.options(row):
            raise ValueError("Этот эпизод находится за пределами соседних сцен.")
        if candidate == self.choices[row]:
            return
        self.undo_stack.append(list(self.choices))
        self.redo_stack.clear()
        self.choices[row] = candidate

    def step(self, row: int, direction: int) -> bool:
        options = self.options(row)
        index = options.index(self.choices[row]) + direction
        if 0 <= index < len(options):
            self.select(row, options[index])
            return True
        return False

    def undo(self, *, redo: bool = False) -> None:
        source, destination = ((self.redo_stack, self.undo_stack) if redo
                               else (self.undo_stack, self.redo_stack))
        if source:
            destination.append(list(self.choices))
            self.choices = source.pop()

    def plan(self) -> RenderPlan:
        replacements = [d for s, d in zip(self.slots, self.choices, strict=True) if d != s.original]
        removed = {s.index for s, d in zip(self.slots, self.choices, strict=True)
                   if d != s.original}
        decisions = []
        for i, d in enumerate(self.base.decisions):
            if i in removed:
                d = d.model_copy(update={"action": "DROP", "output_frame_count": 0,
                    "speed_ramp": None, "provenance": "story:roll_omission"})
            hits = [r for r in replacements if r.source_id == d.source_id
                    and r.source_start_us < d.source_end_us and r.source_end_us > d.source_start_us]
            if hits and d.output_frame_count:
                raise ValueError("Замена пересекает соседний эпизод.")
            bounds = sorted({d.source_start_us, d.source_end_us} | {
                max(d.source_start_us, min(d.source_end_us, t))
                for r in hits for t in (r.source_start_us, r.source_end_us)})
            for a, b in zip(bounds, bounds[1:], strict=False):
                if not any(r.source_start_us <= a and b <= r.source_end_us for r in hits):
                    decisions.append(d.model_copy(update={
                        "source_start_us": a, "source_end_us": b}))
        decisions += replacements
        decisions.sort(key=self.start)
        settings = {k: v for k, v in self.base.settings.items() if k != "request_key"}
        settings.update({"episode_roll": VERSION})
        plan = self.base.model_copy(update={"decisions": decisions, "settings": settings})
        validate_plan(self.project, plan)
        if [d.output_frame_count for d in decisions if d.output_frame_count] != [
                s.original.output_frame_count for s in self.slots]:
            raise ValueError("Замена изменила длительность монтажного места.")
        return plan
