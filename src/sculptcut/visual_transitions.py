"""Conservative visual separation between vetted chronological music episodes."""

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from sculptcut.domain import Project


@dataclass(frozen=True)
class TransitionSettings:
    minimum_difference: float = 8.0
    minimum_quality_ratio: float = 0.8
    signature_size: int = 64
    read_width: int = 480
    cache_entries: int = 128
    tail_us: int = 200_000


SETTINGS = TransitionSettings()
View = NDArray[np.uint8]


def unlocked_window(project: Project, source_id: str, start: int, end: int) -> bool:
    return not any(o.source_id == source_id and o.action != "DROP"
                   and o.start_us < end and o.end_us > start for o in project.overrides)


def view_difference(left: View, right: View) -> float:
    return float(np.mean(np.abs(left.astype(np.int16) - right.astype(np.int16))))


def choose_distinct_view(
    scores: list[float], signature: Callable[[int], View | None], previous: View | None,
    preferred: int, eligible: Callable[[int], bool] | None = None,
) -> int:
    """Replace a near duplicate only with a similarly qualified, visibly distinct option."""
    if previous is None:
        return preferred
    current = signature(preferred)
    if current is None or view_difference(previous, current) >= SETTINGS.minimum_difference:
        return preferred
    floor = scores[preferred] - abs(scores[preferred]) * (1 - SETTINGS.minimum_quality_ratio)
    for index in sorted(range(len(scores)), key=lambda i: (-scores[i], i)):
        if index == preferred or scores[index] < floor or (eligible and not eligible(index)):
            continue
        view = signature(index)
        if view is not None and view_difference(previous, view) >= SETTINGS.minimum_difference:
            return index
    return preferred
