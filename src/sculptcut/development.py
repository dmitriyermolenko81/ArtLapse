"""Persistent local appearance evidence, never geometry or Undo recognition."""

from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass

import cv2
import numpy as np

from sculptcut.analysis import Gray, brightness_histogram
from sculptcut.motion import estimate_motion
from sculptcut.project import cache_key


@dataclass(frozen=True)
class DevelopmentSettings:
    width: int = 240
    state_width: int = 128
    minimum_frames: int = 9
    pixel_delta: int = 10
    persistence_kernel: int = 3
    minimum_tracking_quality: float = 0.5
    maximum_displacement: float = 0.75
    maximum_residual: float = 0.04
    maximum_histogram_change: float = 0.15
    maximum_changed_fraction: float = 0.18
    minimum_persistent_fraction: float = 0.001
    full_weight_fraction: float = 0.025
    minimum_weight: float = 0.85
    maximum_weight: float = 1.6
    repeat_delta: float = 0.008
    repeat_weight: float = 0.65
    candidate_limit: int = 4
    cache_entries: int = 128


SETTINGS = DevelopmentSettings()
DEVELOPMENT_VERSION = "development-1-persistent-local-states"
Window = tuple[str, int, int]


@dataclass(frozen=True)
class DevelopmentEvidence:
    score: float | None = None
    state: Gray | None = None


DevelopmentScorer = Callable[[str, int, int], DevelopmentEvidence]


def development_key() -> str:
    return cache_key(DEVELOPMENT_VERSION, asdict(SETTINGS))


def measure_development(frames: list[Gray]) -> DevelopmentEvidence:
    """Compare temporal medians only when all three views remain comparable."""
    if len(frames) < SETTINGS.minimum_frames:
        return DevelopmentEvidence()
    groups = np.array_split(np.stack(frames), 3)
    before, middle, after = [np.median(group, axis=0).astype(np.uint8) for group in groups]
    for left, right in ((before, middle), (middle, after), (before, after)):
        motion = estimate_motion(left, right)
        histogram_change = float(np.abs(
            brightness_histogram(left) - brightness_histogram(right)
        ).sum()) / 2
        if (
            motion.quality < SETTINGS.minimum_tracking_quality
            or motion.displacement > SETTINGS.maximum_displacement
            or motion.residual > SETTINGS.maximum_residual
            or histogram_change > SETTINGS.maximum_histogram_change
        ):
            return DevelopmentEvidence()
    first_delta = middle.astype(np.int16) - before.astype(np.int16)
    last_delta = after.astype(np.int16) - before.astype(np.int16)
    changed = np.abs(last_delta) > SETTINGS.pixel_delta
    if float(np.mean(changed)) > SETTINGS.maximum_changed_fraction:
        return DevelopmentEvidence()
    persistent = changed & (np.abs(first_delta) > SETTINGS.pixel_delta) & (
        np.sign(first_delta) == np.sign(last_delta)
    )
    persistent = cv2.morphologyEx(
        persistent.astype(np.uint8), cv2.MORPH_OPEN,
        np.ones((SETTINGS.persistence_kernel, SETTINGS.persistence_kernel), dtype=np.uint8),
    )
    fraction = float(np.mean(persistent.astype(np.float32)))
    score = min(1.0, fraction / SETTINGS.full_weight_fraction)
    if fraction < SETTINGS.minimum_persistent_fraction:
        score = 0.0
    state = cv2.resize(
        after, (SETTINGS.state_width, SETTINGS.state_width), interpolation=cv2.INTER_AREA,
    ).astype(np.uint8)
    return DevelopmentEvidence(score, state)


def development_weight(
    evidence: DevelopmentEvidence, previous: DevelopmentEvidence | None = None,
) -> float:
    if evidence.score is None:
        return 1.0
    weight = SETTINGS.minimum_weight + (
        SETTINGS.maximum_weight - SETTINGS.minimum_weight
    ) * evidence.score
    if previous is not None and previous.state is not None and evidence.state is not None:
        motion = estimate_motion(previous.state, evidence.state)
        delta = float(np.mean(
            cv2.absdiff(previous.state, evidence.state).astype(np.float32)
        )) / 255
        if (motion.quality >= SETTINGS.minimum_tracking_quality
                and motion.displacement <= SETTINGS.maximum_displacement
                and delta <= SETTINGS.repeat_delta):
            weight *= SETTINGS.repeat_weight
    return weight


def chronological_weights(
    windows: Iterable[Window], development: DevelopmentScorer | None,
) -> dict[Window, float]:
    result: dict[Window, float] = {}
    previous: dict[str, DevelopmentEvidence] = {}
    for window in windows:
        evidence = development(*window) if development is not None else DevelopmentEvidence()
        result[window] = development_weight(evidence, previous.get(window[0]))
        previous[window[0]] = evidence
    return result
