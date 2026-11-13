"""One source-time partition, independent of every output montage setting."""
import json
from pathlib import Path
from typing import TYPE_CHECKING

from sculptcut.domain import Project
from sculptcut.project import atomic_write, cache_key

VERSION = 'source-catalogue-319'
if TYPE_CHECKING:
    from sculptcut.planning import RenderPlan


def catalogue(root: Path, project: Project) -> list[tuple[str, int, int]]:
    from sculptcut.work_episodes import SETTINGS

    step = round(SETTINGS.source_seconds*1_000_000)
    key = cache_key(VERSION, [(s.id, s.fingerprint, s.duration_us) for s in project.sources],
                    step, project.source_splits)
    path = root/'source-episodes.json'
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if data.get('key') == key:
                return [(str(s), int(a), int(b)) for s, a, b in data['episodes']]
        except (OSError, ValueError, KeyError, TypeError):
            pass  # Rebuild this derived cache without changing project/source data.
    episodes: list[tuple[str, int, int]] = []
    for source in project.sources:
        bounds = sorted({0, source.duration_us, *range(step, source.duration_us, step),
                         *project.source_splits.get(source.id, [])})
        episodes.extend((source.id, a, b) for a, b in zip(bounds, bounds[1:], strict=False))
    atomic_write(path, json.dumps({'version': VERSION, 'key': key, 'episodes': episodes}))
    return episodes


def split_plan(project: Project, plan: 'RenderPlan') -> 'RenderPlan':
    """Project-wide Q seams do not alter output time, crops or ramp phase."""
    from sculptcut.shorts_refill import _split_decision
    from sculptcut.timewarp import output_before

    decisions = []
    for d in plan.decisions:
        if not d.output_frame_count or d.action not in ('KEEP', 'SPEED'):
            decisions.append(d)
            continue
        frames = sorted({output_before(d, t) for t in project.source_splits.get(d.source_id, [])
                         if d.source_start_us < t < d.source_end_us})
        cuts = [(f, f) for f in frames if 0 < f < d.output_frame_count]
        decisions.extend(_split_decision(d, 0, cuts) if cuts else [d])
    return plan.model_copy(update={'decisions': decisions})
