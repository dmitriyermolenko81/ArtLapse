"""Explicitly requested removal of blank screens and regular brush-picker grids."""

from collections.abc import Callable
from pathlib import Path
from threading import Event

import cv2
import numpy as np
from pydantic import Field

from sculptcut.analysis import Gray
from sculptcut.budget import distribute
from sculptcut.domain import Model, Project
from sculptcut.idle import IdleRange
from sculptcut.media import Cancelled, MediaError
from sculptcut.planning import RenderPlan, validate_plan
from sculptcut.project import cache_key, cache_session
from sculptcut.proxy import proxy_path, validated_frame_count
from sculptcut.webcam import AnalysisCapture, analysis_media_key, webcam_roi


class ScreenSettings(Model):
    black: int = Field(default=16, ge=0, le=40)
    white: int = Field(default=240, ge=220, le=255)
    blank_fraction: float = Field(default=0.96, ge=0.9, le=1)
    grid_rows: int = Field(default=4, ge=3)
    grid_columns: int = Field(default=4, ge=4)
    cache_window_seconds: int = Field(default=60, ge=1)


DEFAULT_SCREENS = ScreenSettings()


def screen_kind(gray: Gray, settings: ScreenSettings = DEFAULT_SCREENS) -> str | None:
    h, w = gray.shape
    center = gray[h // 8 : h * 7 // 8, w // 8 : w * 7 // 8]
    if float(np.mean(center <= settings.black)) >= settings.blank_fraction:
        return "black_screen"
    if float(np.mean(center >= settings.white)) >= settings.blank_fraction:
        return "white_screen"
    small = cv2.resize(gray, (960, max(1, round(h * 960 / w)))).astype(np.uint8)
    mask = (small > max(85, float(np.median(small)) + 25)).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8)).astype(np.uint8)
    _, _, stats, centers = cv2.connectedComponentsWithStats(mask)
    points = sorted(
        [
            (float(c[0]), float(c[1]))
            for s, c in zip(stats[1:], centers[1:], strict=True)
            if 4 <= s[2] <= 40 and 4 <= s[3] <= 40 and s[4] >= 12
        ],
        key=lambda p: p[1],
    )
    rows: list[list[tuple[float, float]]] = []
    for point in points:
        if not rows or point[1] - np.mean([p[1] for p in rows[-1]]) > 3:
            rows.append([point])
        else:
            rows[-1].append(point)
    regular = []
    for row in rows:
        xs = sorted(p[0] for p in row)
        xs = [x for i, x in enumerate(xs) if i == 0 or x - xs[i - 1] >= 8]
        if len(xs) >= settings.grid_columns and xs[-1] - xs[0] > 50:
            gaps = np.diff(xs)
            step = float(np.median(gaps))
            if float(np.mean(np.abs(gaps - step) < step * 0.3)) >= 0.65:
                regular.append(float(np.mean([p[1] for p in row])))
    if len(regular) >= settings.grid_rows and regular[-1] - regular[0] > 40:
        return "brush_picker"
    return None


def project_screens(
    root: Path,
    project: Project,
    cancel: Event | None,
    plan: RenderPlan | None = None,
    progress: Callable[[float], None] | None = None,
) -> list[IdleRange]:
    """Scan only retained footage; commit complete windows for cancellation recovery."""
    with cache_session(root) as cache:
        result: list[IdleRange] = []
        completed = 0
        total = (
            sum(s.duration_us for s in project.sources)
            if plan is None
            else sum(d.source_end_us - d.source_start_us
                     for d in plan.decisions if d.action != "DROP")
        )
        for source in project.sources:
            spans: list[tuple[int, int]] = []
            for start, end in sorted(
                [(0, source.duration_us)]
                if plan is None
                else [
                    (d.source_start_us, d.source_end_us)
                    for d in plan.decisions
                    if d.source_id == source.id and d.action != "DROP"
                ]
            ):
                if spans and spans[-1][1] >= start:
                    spans[-1] = (spans[-1][0], max(spans[-1][1], end))
                else:
                    spans.append((start, end))
            if not spans:
                continue
            ranges: list[IdleRange] = []
            capture = AnalysisCapture(
                proxy_path(root, project, source), webcam_roi(project.settings, source.id)
            )
            step = round(1e6 / project.settings.proxy_fps)
            try:
                frame_count = validated_frame_count(capture, source, project.settings.proxy_fps)
                for span_start, span_end in spans:
                    window_size = DEFAULT_SCREENS.cache_window_seconds * 1_000_000
                    for window_start in range(span_start, span_end, window_size):
                        if cancel and cancel.is_set():
                            raise Cancelled("Screen cleanup cancelled")
                        window_end = min(span_end, window_start + window_size)
                        window_id = f"{source.id}:{window_start}:{window_end}"
                        key = cache_key(
                            "screens-4",
                            analysis_media_key(project, source),
                            window_start,
                            window_end,
                            DEFAULT_SCREENS.model_dump(),
                        )
                        cached = cache.get("screen_window", window_id, key)
                        if cached is not None:
                            ranges.extend(IdleRange.model_validate(r) for r in cached["ranges"])
                        else:
                            window_ranges: list[IdleRange] = []
                            first_frame = (
                                window_start * project.settings.proxy_fps + 999_999
                            ) // 1_000_000
                            capture.set(cv2.CAP_PROP_POS_FRAMES, first_frame)
                            frame_index = first_frame
                            while (
                                frame_index < frame_count
                                and round(frame_index * 1_000_000 / project.settings.proxy_fps)
                                < window_end
                            ):
                                if cancel and cancel.is_set():
                                    raise Cancelled("Screen cleanup cancelled")
                                ok, frame = capture.read()
                                if not ok:
                                    raise MediaError(
                                        f"Analysis proxy ended before screen window: {source.path}"
                                    )
                                time_us = round(capture.get(cv2.CAP_PROP_POS_MSEC) * 1000)
                                kind = screen_kind(
                                    cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.uint8)
                                )
                                if kind:
                                    start = max(0, time_us - step)
                                    end = min(source.duration_us, time_us + 2 * step)
                                    if window_ranges and window_ranges[-1].end_us >= start:
                                        window_ranges[-1] = window_ranges[-1].model_copy(
                                            update={"end_us": end}
                                        )
                                    else:
                                        window_ranges.append(
                                            IdleRange(
                                                source_id=source.id,
                                                start_us=start,
                                                end_us=end,
                                                reason=kind,
                                            )
                                        )
                                frame_index += 1
                            if cancel and cancel.is_set():
                                raise Cancelled("Screen cleanup cancelled")
                            cache.put(
                                "screen_window",
                                window_id,
                                key,
                                {
                                    "ranges": [r.model_dump() for r in window_ranges],
                                },
                            )
                            ranges.extend(window_ranges)
                        completed += window_end - window_start
                        if progress:
                            progress(min(1, completed / max(1, total)))
            finally:
                capture.release()
            for value in sorted(ranges, key=lambda r: r.start_us):
                if (
                    result
                    and result[-1].source_id == value.source_id
                    and result[-1].end_us >= value.start_us
                ):
                    result[-1] = result[-1].model_copy(
                        update={"end_us": max(result[-1].end_us, value.end_us)}
                    )
                else:
                    result.append(value)
        return result


def strip_screens(
    project: Project,
    plan: RenderPlan,
    ranges: list[IdleRange],
    *,
    provenance: str = "user:remove_screen",
    warning: str = "Removed blank screens and brush menus by user request",
) -> RenderPlan:
    if not ranges:
        return plan
    merged: list[IdleRange] = []
    for r in sorted(ranges, key=lambda r: (r.source_id, r.start_us)):
        if merged and merged[-1].source_id == r.source_id and merged[-1].end_us >= r.start_us:
            merged[-1] = merged[-1].model_copy(update={"end_us": max(merged[-1].end_us, r.end_us)})
        else:
            merged.append(r)
    ranges = merged
    decisions = []
    weights = []
    for d in plan.decisions:
        hits = [
            r
            for r in ranges
            if r.source_id == d.source_id
            and r.start_us < d.source_end_us
            and r.end_us > d.source_start_us
        ]
        cuts = sorted(
            {d.source_start_us, d.source_end_us}
            | {max(d.source_start_us, r.start_us) for r in hits}
            | {min(d.source_end_us, r.end_us) for r in hits}
        )
        for a, b in zip(cuts, cuts[1:], strict=False):
            unwanted = any(r.start_us < b and r.end_us > a for r in hits)
            drop = d.action == "DROP" or unwanted
            decisions.append(
                d.model_copy(
                    update={
                        "source_start_us": a,
                        "source_end_us": b,
                        "action": "DROP" if drop else d.action,
                        "output_frame_count": 0 if drop else 1,
                        "provenance": provenance if unwanted else d.provenance,
                    }
                )
            )
            weights.append(
                max(1e-6, d.output_frame_count * (b - a) / (d.source_end_us - d.source_start_us))
            )
    caps = [
        0
        if d.action == "DROP"
        else max(1, (d.source_end_us - d.source_start_us) * plan.output_fps // 1_000_000)
        for d in decisions
    ]
    target = min(plan.actual_frame_count, sum(caps))
    minima = [int(c > 0) for c in caps]
    maxima = caps
    if not target:
        raise ValueError("No sculpting footage remains after screen cleanup")
    counts = distribute(minima, weights, maxima, max(sum(minima), target))
    result = plan.model_copy(
        update={
            "decisions": [
                d.model_copy(update={"output_frame_count": n})
                for d, n in zip(decisions, counts, strict=True)
            ],
            "actual_frame_count": sum(counts),
            "settings": plan.settings
            | {
                "final_view_frames": sum(
                    n
                    for d, n in zip(decisions, counts, strict=True)
                    if d.provenance == "user:shorts_final"
                )
            }
            if plan.settings.get("story") == "process"
            else plan.settings,
            "warnings": plan.warnings
            + [warning]
            + (
                ["Selected footage shorter than target; no filler added"]
                if sum(counts) < plan.target_frame_count
                else []
            ),
        }
    )
    validate_plan(project, result)
    return result
