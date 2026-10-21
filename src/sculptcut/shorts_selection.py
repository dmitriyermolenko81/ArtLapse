"""Bounded visual shortlist scoring; scores are not geometry or Undo evidence."""

from dataclasses import dataclass
from pathlib import Path
from threading import Event

import cv2
import numpy as np

from sculptcut.domain import Project, SourceMedia
from sculptcut.media import Cancelled
from sculptcut.proxy import proxy_path
from sculptcut.roi import pixel_bounds
from sculptcut.screen_cleanup import screen_kind
from sculptcut.segmentation import Segment
from sculptcut.webcam import AnalysisCapture, webcam_roi


@dataclass(frozen=True)
class SelectionSettings:
    candidate_count: int = 8
    samples: int = 11
    width: int = 480
    pixel_delta: int = 14
    minimum_change: float = 0.001
    flow_pixels: float = 1.5
    broad_motion_fraction: float = 0.35
    return_fraction: float = 0.15


DEFAULT_SELECTION = SelectionSettings()


def visual_score(
    reader: cv2.VideoCapture | AnalysisCapture,
    start: int,
    length: int,
    cancel: Event | None,
    settings: SelectionSettings = DEFAULT_SELECTION,
) -> float:
    from sculptcut.shorts import viewport_box

    frames = []
    viewport = None
    for offset in np.linspace(0, max(0, length - 200_000), settings.samples):
        if cancel and cancel.is_set():
            raise Cancelled("Shorts candidate selection cancelled")
        reader.set(cv2.CAP_PROP_POS_MSEC, (start + float(offset)) / 1000)
        ok, frame = reader.read()
        if not ok:
            return 0.0
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.uint8)
        if screen_kind(gray):
            return 0.0
        if viewport is None:
            viewport = viewport_box(gray)
        if viewport is None:
            return 0.0
        x, y, r, b = pixel_bounds(viewport, gray.shape[1], gray.shape[0])
        gray = cv2.resize(gray[y:b, x:r], (settings.width, settings.width)).astype(np.uint8)
        frames.append(cv2.GaussianBlur(gray, (5, 5), 0).astype(np.uint8))
    background = float(np.median(frames[0]))
    foreground = np.abs(frames[0].astype(float) - background) > settings.pixel_delta
    if np.count_nonzero(foreground) < settings.width:
        return 0.0
    changes, movements = [], []
    for before, after in zip(frames, frames[1:], strict=False):
        changes.append(float(np.mean(cv2.absdiff(before, after) > settings.pixel_delta)))
        flow = cv2.calcOpticalFlowFarneback(
            before,
            after,
            np.zeros((*before.shape, 2), dtype=np.float32),
            0.5,
            3,
            15,
            3,
            5,
            1.2,
            0,
        )
        magnitude = np.linalg.norm(flow, axis=2)
        movements.append(float(np.mean(magnitude[foreground] > settings.flow_pixels)))
    activity = float(np.mean(changes))
    if activity < settings.minimum_change:
        return 0.0
    movement = float(np.mean(movements))
    # Rotations need not fit one affine transform: widespread optical flow is
    # downweighted even with a large affine residual. Local edits can still move.
    motion_penalty = max(0.02, 1 - movement / settings.broad_motion_fraction)
    net = float(np.mean(cv2.absdiff(frames[0], frames[-1]) > settings.pixel_delta))
    excursion = max(
        float(np.mean(cv2.absdiff(frames[0], frame) > settings.pixel_delta)) for frame in frames
    )
    # Returning to similar pixels reduces highlight priority, never marks Undo.
    return_penalty = 0.1 if net < excursion * settings.return_fraction else 1.0
    return min(activity, 0.15) * motion_penalty * return_penalty


def choose_start(
    root: Path,
    project: Project,
    source: SourceMedia,
    a: int,
    b: int,
    length: int,
    segments: list[Segment],
    cancel: Event | None,
) -> int | None:
    settings = DEFAULT_SELECTION
    starts = {round(float(t)) for t in np.linspace(a, b - length, settings.candidate_count)}
    relevant = [s for s in segments if s.source_id == source.id and s.start_us < b and s.end_us > a]
    for segment in sorted(relevant, key=lambda s: s.importance, reverse=True)[
        : settings.candidate_count
    ]:
        starts.add(max(a, min(b - length, (segment.start_us + segment.end_us - length) // 2)))

    def rank(start: int) -> float:
        weights = {"SCULPT": 1.0, "UNKNOWN": 0.3, "MIXED": 0.2}
        return sum(
            max(0, min(start + length, s.end_us) - max(start, s.start_us))
            * weights.get(s.kind, 0.0)
            * (0.5 + s.importance)
            for s in relevant
        )

    allowed = [
        start
        for start in starts
        if not any(
            o.source_id == source.id
            and o.action == "DROP"
            and o.start_us < start + length
            and o.end_us > start
            for o in project.overrides
        )
    ]
    shortlist = sorted(allowed, key=lambda start: (-rank(start), start))[: settings.candidate_count]
    reader = AnalysisCapture(
        proxy_path(root, project, source), webcam_roi(project.settings, source.id)
    )
    try:
        from sculptcut.sculpt_only import strict_score

        scorer = strict_score if project.settings.sculpt_only else visual_score
        scores = [(scorer(reader, start, length, cancel), start) for start in shortlist]
    finally:
        reader.release()
    if not scores:
        return None
    score, start = max(scores, key=lambda pair: (pair[0], -pair[1]))
    return start if score > settings.minimum_change else None
