"""Read-only scene-roll preparation, safe to execute outside the GUI thread."""

import sqlite3
from bisect import bisect_right
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory
from threading import Event

from sculptcut.combined_edits import CombinedEdit, prepare_combined_edit
from sculptcut.domain import Project
from sculptcut.episode_roll import EpisodeRoll, catalogue_episodes, catalogue_snapshot
from sculptcut.intro import body_start_frame, output_frame_count
from sculptcut.media import Cancelled
from sculptcut.output_cuts import edited_time, retained_ranges
from sculptcut.planning import RenderDecision, RenderPlan
from sculptcut.project import AnalysisCache, load_project
from sculptcut.shorts_refill import cuts_on_plan_clock
from sculptcut.timewarp import output_before
from sculptcut.variants import read_variant
from sculptcut.work_episodes import process_cache_key


@dataclass(frozen=True)
class RollContext:
    edit: CombinedEdit
    roll: EpisodeRoll
    video_spans: list[tuple[int, int]]
    duration_ms: int


@contextmanager
def strength_cache(root: Path) -> Iterator[AnalysisCache]:
    with TemporaryDirectory(prefix="artlapse-choice-cache-") as scratch:
        path = root / "analysis.sqlite"
        wal = path.with_name(path.name + "-wal")
        immutable = not wal.is_file() or wal.stat().st_size == 0
        if not immutable:
            # SQLite may need a writable -shm file even for a read-only WAL
            # connection. Read a private copy; browsing never writes to the project.
            copyfile(path, Path(scratch)/"analysis.sqlite")
            copyfile(wal, Path(scratch)/"analysis.sqlite-wal")
            path = Path(scratch)/"analysis.sqlite"
        uri = path.resolve().as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            yield AnalysisCache(connection)


def load_roll_context(root: Path, video: Path, cuts: list[tuple[int, int]],
                      cancel: Event) -> RollContext:
    def check() -> None:
        if cancel.is_set():
            raise Cancelled("Загрузка эпизодов отменена")

    check()
    state = read_variant(video)
    edit = prepare_combined_edit(root, video, cuts, apply_current=False, exact_variant=True)
    check()
    snapshot = state.get("episode_catalogue") or catalogue_snapshot(root, edit.project, edit.plan)
    catalogue = catalogue_episodes(snapshot, edit.project)
    if not catalogue:
        raise ValueError("Для замены нужен каталог рабочих эпизодов этого таймлапса.")
    # Older catalogues stored a combined importance score. Recover the exact
    # persistent-change measurement without decoding video or mutating the cache.
    if (root / "analysis.sqlite").is_file():
        with strength_cache(root) as cache:
            for i, e in enumerate(catalogue):
                check()
                if (e.process_strength is not None or e.work_start_us is None
                        or e.work_end_us is None):
                    continue
                value = cache.get("work_process",
                    f"{e.source_id}:{e.work_start_us}:{e.work_end_us}",
                    process_cache_key(edit.project, e.source_id, e.work_start_us, e.work_end_us))
                if value is not None and value.get("score") is not None:
                    catalogue[i] = e.model_copy(update={"process_strength": float(value["score"])})
    drops = [o for o in load_project(root).overrides if o.action == "DROP"]
    project = edit.project.model_copy(update={"overrides": [*edit.project.overrides, *drops]})
    roll = EpisodeRoll(project, edit.plan, catalogue)
    check()
    original = RenderPlan.model_validate(state["plan"])
    saved_project = Project.model_validate(state["project"])
    fps = original.output_fps
    total = output_frame_count(saved_project.settings, fps, original.actual_frame_count)
    ranges = retained_ranges((total*1_000_000+fps-1)//fps,
                              cuts_on_plan_clock(video, saved_project, original, []))
    position = body_start_frame(saved_project.settings, fps, original.actual_frame_count)
    # Index each source once instead of rescanning all dropped parts per scene.
    indexed: dict[str, list[tuple[int, int, RenderDecision]]] = {}
    for d in original.decisions:
        check()
        if d.output_frame_count:
            indexed.setdefault(d.source_id, []).append((d.source_start_us, position, d))
        position += d.output_frame_count
    starts = {sid: [a for a, _, _ in rows] for sid, rows in indexed.items()}
    spans = []
    for slot in roll.slots:
        check()
        d = slot.original
        index = bisect_right(starts.get(d.source_id, []), d.source_start_us)-1
        if index < 0:
            raise ValueError("У этой сцены нет соответствия в готовом ролике.")
        _, frame, old = indexed[d.source_id][index]
        if not old.source_start_us <= d.source_start_us < old.source_end_us:
            raise ValueError("У этой сцены нет соответствия в готовом ролике.")
        start = frame+(0 if old.action == "HOLD" else output_before(old, d.source_start_us))
        a = edited_time(ranges, start*1_000_000//fps)//1000
        spans.append((a, a+round(d.output_frame_count/fps*1000)))
    return RollContext(edit, roll, spans, sum(b-a for a,b in ranges)//1000)
