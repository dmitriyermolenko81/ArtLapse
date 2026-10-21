"""Strict local-edit selection within the foreground, with explicit uncertainty."""

from dataclasses import dataclass
from pathlib import Path
from threading import Event

import cv2
import numpy as np

from sculptcut.analysis import Gray
from sculptcut.domain import Project, SourceMedia
from sculptcut.media import Cancelled, MediaError
from sculptcut.proxy import proxy_path
from sculptcut.roi import pixel_bounds
from sculptcut.screen_cleanup import screen_kind
from sculptcut.webcam import AnalysisCapture, webcam_roi


@dataclass(frozen=True)
class SculptSettings:
    width: int = 384
    step_us: int = 200_000
    minimum_run_us: int = 600_000
    navigation_guard_us: int = 400_000
    pixel_delta: int = 10
    minimum_change: float = 0.003
    maximum_change: float = 0.16
    motion_pixels: float = 0.8
    maximum_moving_fraction: float = 0.12
    minimum_subject_fraction: float = 0.008
    final_search_us: int = 30_000_000
    context_us: int = 5_000_000


SETTINGS = SculptSettings()


def subject_mask(gray: Gray) -> Gray:
    mode = int(np.bincount((gray // 4).ravel(), minlength=64).argmax())
    mask = (np.abs(gray.astype(float) - mode * 4 - 2) > SETTINGS.pixel_delta).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), dtype=np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    result = np.zeros_like(gray)
    if contours:
        largest = max(contours, key=cv2.contourArea)
        if cv2.contourArea(largest) >= gray.size * SETTINGS.minimum_subject_fraction:
            cv2.drawContours(result, [largest], -1, 255, cv2.FILLED)
    return cv2.erode(result, np.ones((5, 5), dtype=np.uint8)).astype(np.uint8)


def pair_kind(before: Gray, after: Gray) -> str:
    mask = (subject_mask(before) > 0) & (subject_mask(after) > 0)
    if np.count_nonzero(mask) < before.size * SETTINGS.minimum_subject_fraction:
        return "unknown"
    delta = cv2.absdiff(before, after)
    changed = float(np.mean(delta[mask] > SETTINGS.pixel_delta))
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
    moved = float(np.mean(np.linalg.norm(flow, axis=2)[mask] > SETTINGS.motion_pixels))
    if moved > SETTINGS.maximum_moving_fraction or changed > SETTINGS.maximum_change:
        return "navigation"
    if changed < SETTINGS.minimum_change:
        return "static"
    return "local"


def inspect_window(
    reader: cv2.VideoCapture | AnalysisCapture,
    start: int,
    end: int,
    cancel: Event | None,
) -> list[tuple[int, int, str]]:
    from sculptcut.shorts import viewport_box

    reader.set(cv2.CAP_PROP_POS_MSEC, start / 1000)
    previous: Gray | None = None
    previous_time = start
    first_frames: list[Gray] = []
    last_frames: list[Gray] = []
    viewport = None
    result = []
    fps = reader.get(cv2.CAP_PROP_FPS)
    if fps <= 0:
        raise MediaError("Cannot read sculpt-only proxy FPS")
    # Sequential decode of only this short candidate, never repeated GOP seeks.
    index = 0
    while start + round(index * 1e6 / fps) < end:
        if cancel and cancel.is_set():
            raise Cancelled("Sculpt-only selection cancelled")
        ok, frame = reader.read()
        if not ok:
            raise MediaError("Cannot decode sculpt-only candidate")
        timestamp = start + round(index * 1e6 / fps)
        index += 1
        if previous is not None and timestamp - previous_time < SETTINGS.step_us:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.uint8)
        blocked = screen_kind(gray) is not None
        viewport = viewport or viewport_box(gray)
        if viewport is None or blocked:
            result.append((previous_time, min(end, timestamp + SETTINGS.step_us), "unknown"))
            previous = None
            previous_time = timestamp
            continue
        x, y, r, b = pixel_bounds(viewport, gray.shape[1], gray.shape[0])
        gray = cv2.resize(gray[y:b, x:r], (SETTINGS.width, SETTINGS.width)).astype(np.uint8)
        gray = cv2.GaussianBlur(gray, (5, 5), 0).astype(np.uint8)
        if len(first_frames) < 3:
            first_frames.append(gray)
        last_frames = (last_frames + [gray])[-3:]
        if previous is not None:
            result.append((previous_time, timestamp, pair_kind(previous, gray)))
        previous, previous_time = gray, timestamp
    if previous_time < end:
        result.append((previous_time, end, "unknown"))
    if len(first_frames) == 3 and len(last_frames) == 3:
        first = np.median(np.stack(first_frames), axis=0).astype(np.uint8)
        last = np.median(np.stack(last_frames), axis=0).astype(np.uint8)
        mask = (subject_mask(first) > 0) & (subject_mask(last) > 0)
        persistent = (
            float(np.mean(cv2.absdiff(first, last)[mask] > SETTINGS.pixel_delta))
            if mask.any()
            else 0
        )
        if persistent < SETTINGS.minimum_change:
            result = [(a, b, "unknown" if kind == "local" else kind) for a, b, kind in result]
    navigation = [(a, b) for a, b, kind in result if kind == "navigation"]
    return [
        (
            a,
            b,
            "navigation"
            if any(
                left - SETTINGS.navigation_guard_us < b and right + SETTINGS.navigation_guard_us > a
                for left, right in navigation
            )
            else kind,
        )
        for a, b, kind in result
    ]


def runs(rows: list[tuple[int, int, str]], kind: str) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for a, b, label in rows:
        if label != kind:
            continue
        if merged and merged[-1][1] == a:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return [(a, b) for a, b in merged if b - a >= SETTINGS.minimum_run_us]


def strict_score(
    reader: cv2.VideoCapture | AnalysisCapture, start: int, length: int, cancel: Event | None,
) -> float:
    return (
        sum(b - a for a, b in runs(inspect_window(reader, start, start + length, cancel), "local"))
        / length
    )


def final_start(
    root: Path,
    project: Project,
    source: SourceMedia,
    length: int,
    cancel: Event | None,
    minimum_start: int = 0,
) -> int | None:
    reader = AnalysisCapture(
        proxy_path(root, project, source), webcam_roi(project.settings, source.id)
    )
    try:
        best: tuple[int, int] | None = None
        for start in np.linspace(
            max(minimum_start, source.duration_us - SETTINGS.final_search_us),
            source.duration_us - length,
            8,
        ):
            a = round(float(start))
            if any(
                o.source_id == source.id
                and o.action == "DROP"
                and o.start_us < a + length
                and o.end_us > a
                for o in project.overrides
            ):
                continue
            stable = runs(inspect_window(reader, a, a + length, cancel), "static")
            for left, right in stable:
                if right - left >= 3_000_000:
                    candidate = (right - left, left)
                    if best is None or candidate > best:
                        best = candidate
        return best[1] if best else None
    finally:
        reader.release()
