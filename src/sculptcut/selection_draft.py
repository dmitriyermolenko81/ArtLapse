"""Atomic, per-variant edit drafts, separate from project analysis and video exports."""

import hashlib
import json
import math
from dataclasses import replace
from pathlib import Path

from sculptcut.domain import Override, Project
from sculptcut.planning import RenderPlan, validate_plan
from sculptcut.project import atomic_write, load_project
from sculptcut.selection_duration import fit_selection
from sculptcut.source_selection import SourceSelection


def draft_path(root: Path, video: Path) -> Path:
    key = hashlib.sha256(str(video.resolve()).casefold().encode()).hexdigest()[:20]
    return root / "edit-drafts" / f"{key}.json"


def save_navigation(root: Path, video: Path, cursor: int, view: tuple[float, float],
                    revision: tuple[int, int]) -> None:
    path = draft_path(root, video)
    stat = path.stat()
    if (stat.st_size, stat.st_mtime_ns) != revision:
        raise ValueError("Проект изменился вне редактора подрезки.")
    atomic_write(path.with_suffix('.position.json'), json.dumps({
        'revision': list(revision), 'cursor': cursor, 'view': view}))


def save_draft(root: Path, video: Path, session: SourceSelection, cursor: int,
               view: tuple[float, float], expected: list[Override] | None = None) -> None:
    stat = video.stat()
    current = load_project(root)
    if expected is not None and current.overrides != expected:
        raise ValueError("Проект изменился вне редактора подрезки.")
    data = {"version": 1, "video": str(video.resolve()), "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns, "expected_overrides":
            [o.model_dump(mode="json") for o in current.overrides],
            "expected_splits": current.source_splits,
            "base": session.base.model_dump(mode="json"),
            "reference": session.reference.model_dump(mode="json"),
            "plan": session.plan.model_dump(mode="json"),
            "project": session.project.model_dump(mode="json"),
            "cursor": cursor, "view": view, "cuts": session.original.cuts,
            "leading": session.original.leading, "trailing": session.original.trailing,
            "undo": [[p.model_dump(mode="json"), project.model_dump(mode="json")]
                     for p, project in session.undo_stack[-10:]],
            "redo": [[p.model_dump(mode="json"), project.model_dump(mode="json")]
                     for p, project in session.redo_stack[-10:]]}
    atomic_write(draft_path(root, video), json.dumps(data, ensure_ascii=False))


def restore_draft(root: Path, video: Path, session: SourceSelection) -> None:
    path = draft_path(root, video)
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    stat = video.stat()
    current = load_project(root)
    if data.get('expected_splits', {}) != current.source_splits:
        raise ValueError('Общие разрезы проекта изменились вне редактора.')
    if (data["version"] != 1 or data["video"] != str(video.resolve())
            or data["size"] != stat.st_size or data["mtime_ns"] != stat.st_mtime_ns
            or data["expected_overrides"] != [
                o.model_dump(mode="json") for o in current.overrides]):
        raise ValueError("Сохранённая подрезка относится к другой версии проекта или видео.")
    project = Project.model_validate(data["project"])
    if [(s.id, s.fingerprint) for s in project.sources] != [
            (s.id, s.fingerprint) for s in current.sources]:
        raise ValueError("Исходники сохранённой подрезки изменились.")
    def pair(raw_plan: object, raw_project: object) -> tuple[RenderPlan, Project]:
        p = RenderPlan.model_validate(raw_plan)
        proj = Project.model_validate(raw_project).model_copy(update={"sources": current.sources})
        validate_plan(proj, p)
        return p, proj
    plan, project = pair(data["plan"], data["project"])
    base, _ = pair(data["base"], data["project"])
    reference, _ = pair(data.get('reference', session.reference.model_dump()), data['project'])
    undo = [pair(p, proj) for p, proj in data["undo"]]
    redo = [pair(p, proj) for p, proj in data["redo"]]
    cursor = max(0, int(data["cursor"]))
    view = float(data["view"][0]), float(data["view"][1])
    if not all(math.isfinite(v) and v >= 0 for v in view):
        raise ValueError("Неверный масштаб сохранённой подрезки.")
    # Navigation-only autosaves must never serialize the montage/history again.
    # A sidecar can attach only to the exact validated full draft revision.
    try:
        position = json.loads(path.with_suffix('.position.json').read_text(encoding='utf-8'))
        stat = path.stat()
        if position['revision'] == [stat.st_size, stat.st_mtime_ns]:
            point = max(0, int(position['cursor']))
            region = float(position['view'][0]), float(position['view'][1])
            if all(math.isfinite(v) and v >= 0 for v in region):
                cursor, view = point, region
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        pass  # The full edit draft remains valid if optional navigation is lost.
    session.original = replace(session.original, cuts=[tuple(c) for c in data["cuts"]],
        leading=[tuple(c) for c in data["leading"]], trailing=[tuple(c) for c in data["trailing"]])
    session.base, session.plan, session.project = base, plan, project
    session.reference = reference
    session.fixed_duration = (session.fixed_duration
                              or plan.settings.get('selection_duration_policy') == 'fixed')
    if session.fixed_duration and session.changed:
        session.plan = fit_selection(project, plan, session.original.leading,
                                     session.original.trailing)
    session.target = base.actual_frame_count
    session.undo_stack, session.redo_stack = undo, redo
    session.restored_cursor = cursor
    session.restored_view = view
