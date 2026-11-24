"""Atomic, per-variant edit drafts, separate from project analysis and video exports."""

import hashlib
import json
import math
import os
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
                    revision: tuple[int, int] | None) -> None:
    path = draft_path(root, video)
    stat = path.stat() if path.is_file() else None
    current = (stat.st_size, stat.st_mtime_ns) if stat else None
    if current != revision:
        raise ValueError("Проект изменился вне редактора подрезки.")
    video_stat = video.stat()
    atomic_write(path.with_suffix('.position.json'), json.dumps({
        'revision': revision, 'video': [video_stat.st_size, video_stat.st_mtime_ns],
        'cursor': cursor, 'view': view}))


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


def restore_draft(root: Path, video: Path, session: SourceSelection, *,
                  pending_cuts: bool = False) -> None:
    path = draft_path(root, video)
    try:
        draft_file = path.open(encoding="utf-8")
    except FileNotFoundError:
        if not pending_cuts:
            _restore_navigation(path, video, session)
        return
    with draft_file:
        stat = os.fstat(draft_file.fileno())
        revision = stat.st_size, stat.st_mtime_ns
        data = json.load(draft_file)
    if not isinstance(data, dict):
        raise ValueError('Повреждённый черновик подрезки.')
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
    session.original = replace(session.original, cuts=[tuple(c) for c in data["cuts"]],
        leading=[tuple(c) for c in data["leading"]], trailing=[tuple(c) for c in data["trailing"]])
    session.base, session.plan, session.project = base, plan, project
    session.reference = reference
    session.fixed_duration = (session.fixed_duration
                              or plan.settings.get('selection_duration_policy') == 'fixed')
    if session.keep_duration and session.changed:
        session.plan = fit_selection(project, plan, session.original.leading,
                                     session.original.trailing)
    session.target = base.actual_frame_count
    session.undo_stack, session.redo_stack = undo, redo
    session.restored_cursor = cursor
    session.restored_view = view
    session.draft_revision = revision
    _restore_navigation(path, video, session)


def _restore_navigation(path: Path, video: Path, session: SourceSelection) -> None:
    try:
        position = json.loads(path.with_suffix('.position.json').read_text(encoding='utf-8'))
        revision = session.draft_revision
        if revision is not None:
            if position['revision'] != list(revision):
                return
        else:
            stat = video.stat()
            if position['revision'] is not None or position['video'] != [
                stat.st_size, stat.st_mtime_ns,
            ]:
                return
        cursor = max(0, int(position['cursor']))
        view = float(position['view'][0]), float(position['view'][1])
        if all(math.isfinite(value) and value >= 0 for value in view):
            session.restored_cursor = cursor
            session.restored_view = view
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        pass  # Position is optional and may belong to an earlier revision.
