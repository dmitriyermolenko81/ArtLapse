"""Conservative checks of selected timelapse footage, independently of static UI."""

from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Event

import cv2
import numpy as np
from numpy.typing import NDArray

from sculptcut.analysis import Gray
from sculptcut.domain import Project
from sculptcut.idle import IdleRange
from sculptcut.media import Cancelled, MediaError
from sculptcut.planning import RenderPlan
from sculptcut.project import cache_key, cache_session
from sculptcut.proxy import proxy_path, validated_frame_count
from sculptcut.roi import pixel_bounds
from sculptcut.screen_cleanup import strip_screens
from sculptcut.shorts import viewport_box
from sculptcut.webcam import AnalysisCapture, analysis_media_key, webcam_roi


@dataclass(frozen=True)
class CleanupSettings:
    features: int = 300
    navigation_widgets_fraction: float = 0.18
    matches: int = 12
    match_ratio: float = 0.7
    inlier_ratio: float = 0.92
    displacement: float = 2.0
    residual: float = 0.008
    changed_fraction: float = 0.02
    changed_level: int = 12
    local_quantile: float = 0.995
    local_residual: float = 0.08
    minimum_coverage: float = 0.01
    camera_min_seconds: float = 1.2
    camera_context_seconds: float = 0.4
    bounds_padding: int = 20
    stable_detail: float = 0.002
    stable_edges: float = 0.002
    stable_changed_fraction: float = 0.0005
    appearance_width: int = 240


SETTINGS = CleanupSettings()
Signature = tuple[NDArray[np.float32], NDArray[np.float32] | None]


def conservative_camera_ranges(ranges: list[IdleRange]) -> list[IdleRange]:
    """Keep brief or interrupted movements, plus both sides of longer movements."""
    cameras: list[IdleRange] = []
    others = [r for r in ranges if r.reason != "timelapse_camera_only"]
    for value in sorted((r for r in ranges if r.reason == "timelapse_camera_only"),
                        key=lambda r: (r.source_id, r.start_us)):
        if (cameras and cameras[-1].source_id == value.source_id
                and cameras[-1].end_us >= value.start_us):
            cameras[-1] = cameras[-1].model_copy(update={
                "end_us": max(cameras[-1].end_us, value.end_us),
            })
        else:
            cameras.append(value)
    context = round(SETTINGS.camera_context_seconds * 1_000_000)
    minimum = round(SETTINGS.camera_min_seconds * 1_000_000)
    return sorted(others + [r.model_copy(update={
        "start_us": r.start_us + context, "end_us": r.end_us - context,
    }) for r in cameras if r.end_us - r.start_us >= minimum
        and r.end_us - r.start_us > 2 * context], key=lambda r: (r.source_id, r.start_us))


def signature(gray: Gray) -> Signature:
    viewport = viewport_box(gray)
    if viewport is None:
        return np.empty((0, 2), dtype=np.float32), None
    x, y, right, bottom = pixel_bounds(viewport, gray.shape[1], gray.shape[0])
    mask = np.zeros_like(gray)
    # ZBrush's navigation head and preview tile live above the working model.
    mask[y + round((bottom - y) * SETTINGS.navigation_widgets_fraction):bottom - 3,
         x + 3:right - 3] = 255
    points, descriptors = cv2.SIFT.create(nfeatures=SETTINGS.features).detectAndCompute(gray, mask)
    return (
        np.asarray([point.pt for point in points], dtype=np.float32).reshape(-1, 2),
        np.asarray(descriptors, dtype=np.float32) if descriptors is not None else None,
    )


def camera_only(
    previous: Gray, current: Gray, before: Signature, after: Signature,
    settings: CleanupSettings = SETTINGS,
) -> bool:
    """Require coherent scale/translation AND small remaining image changes."""
    a, descriptors_a = before
    b, descriptors_b = after
    if descriptors_a is None or descriptors_b is None or len(b) < 2:
        return False
    matches = cv2.BFMatcher().knnMatch(descriptors_a, descriptors_b, k=2)
    good = [
        first for pair in matches if len(pair) == 2 for first, second in [pair]
        if first.distance < settings.match_ratio * second.distance
    ]
    if len(good) < settings.matches:
        return False
    old = a[[match.queryIdx for match in good]]
    new = b[[match.trainIdx for match in good]]
    cv2.setRNGSeed(42)
    matrix, inliers = cv2.estimateAffinePartial2D(
        old, new, method=cv2.RANSAC, ransacReprojThreshold=2, maxIters=2000,
    )
    if matrix is None or inliers is None:
        return False
    selected = np.asarray(inliers).ravel() > 0
    if int(selected.sum()) < settings.matches or float(selected.mean()) < settings.inlier_ratio:
        return False
    if float(np.median(np.linalg.norm(old[selected] - new[selected], axis=1))) <= (
        settings.displacement
    ):
        return False
    points = new[selected]
    height, width = current.shape
    if cv2.contourArea(cv2.convexHull(points)) < width * height * settings.minimum_coverage:
        return False
    left, top = np.maximum(0, points.min(axis=0) - settings.bounds_padding).astype(int)
    right, bottom = np.minimum(
        [width, height], points.max(axis=0) + settings.bounds_padding,
    ).astype(int)
    warped = cv2.warpAffine(previous, matrix, (width, height))
    valid = cv2.warpAffine(np.full_like(previous, 255), matrix, (width, height)) > 250
    valid = valid[top:bottom, left:right]
    if not valid.any():
        return False
    delta = np.asarray(cv2.absdiff(
        cv2.GaussianBlur(warped, (3, 3), 0), cv2.GaussianBlur(current, (3, 3), 0),
    ), dtype=np.uint8)[top:bottom, left:right][valid]
    return (
        float(delta.mean()) / 255 <= settings.residual
        and float(np.mean(delta > settings.changed_level)) <= settings.changed_fraction
        and float(np.quantile(delta, settings.local_quantile)) / 255 <= settings.local_residual
    )


def appearance(gray: Gray) -> tuple[NDArray[np.float32], Gray]:
    small = cv2.resize(
        gray, (SETTINGS.appearance_width, max(1, round(gray.shape[0] *
                                                     SETTINGS.appearance_width / gray.shape[1]))),
        interpolation=cv2.INTER_AREA,
    )
    smooth = cv2.GaussianBlur(small.astype(np.float32), (0, 0), 4)
    detail = (small.astype(np.float32) - smooth).astype(np.float32)
    return detail, cv2.Canny(small, 40, 100).astype(np.uint8)


def stable(
    before: tuple[NDArray[np.float32], Gray], after: tuple[NDArray[np.float32], Gray],
) -> bool:
    # Smooth animated backgrounds do not turn a stationary placeholder into work.
    return (
        float(np.mean(np.abs(before[0] - after[0]))) / 255 <= SETTINGS.stable_detail
        and float(np.mean(np.abs(before[0] - after[0]) > SETTINGS.changed_level)) <= (
            SETTINGS.stable_changed_fraction
        )
        and float(np.mean(before[1] != after[1])) <= SETTINGS.stable_edges
    )


def cleanup_ranges(
    root: Path, project: Project, plan: RenderPlan, cancel: Event | None,
    progress: Callable[[float], None] | None = None,
) -> list[IdleRange]:
    if cancel and cancel.is_set():
        raise Cancelled("Timelapse cleanup cancelled")
    with cache_session(root) as cache:
        ranges: list[IdleRange] = []
        total_us = max(1, sum(d.source_end_us - d.source_start_us for d in plan.decisions
                             if d.action != "DROP"))
        completed_us = 0
        for source in project.sources:
            if cancel and cancel.is_set():
                raise Cancelled("Timelapse cleanup cancelled")
            spans: list[tuple[int, int]] = []
            for decision in plan.decisions:
                if decision.source_id != source.id or decision.action == "DROP":
                    continue
                if spans and spans[-1][1] == decision.source_start_us:
                    spans[-1] = spans[-1][0], decision.source_end_us
                else:
                    spans.append((decision.source_start_us, decision.source_end_us))
            key = cache_key("timelapse-cleanup-1", analysis_media_key(project, source), spans,
                            asdict(SETTINGS), project.settings.timelapse_static_cut_seconds)
            cached = cache.get("timelapse_cleanup", source.id, key)
            if cached:
                ranges.extend(IdleRange.model_validate(value) for value in cached["ranges"])
                completed_us += sum(end - start for start, end in spans)
                if progress:
                    progress(min(1, completed_us / total_us))
                continue
            selected_ranges: list[IdleRange] = []
            reader = AnalysisCapture(
                proxy_path(root, project, source), webcam_roi(project.settings, source.id)
            )
            fps = project.settings.proxy_fps
            step = round(1_000_000 / fps)
            minimum_static = round(project.settings.timelapse_static_cut_seconds * 1_000_000)
            try:
                frame_count = validated_frame_count(reader, source, fps)
                for start, end in spans:
                    if cancel and cancel.is_set():
                        raise Cancelled("Timelapse cleanup cancelled")
                    window_id = f"{source.id}:{start}:{end}"
                    window_key = cache_key(
                        "timelapse-window-1", analysis_media_key(project, source),
                        start, end, asdict(SETTINGS), minimum_static,
                    )
                    window_cache = cache.get("timelapse_window", window_id, window_key)
                    if window_cache:
                        selected_ranges.extend(IdleRange.model_validate(value)
                                               for value in window_cache["ranges"])
                        completed_us += end - start
                        if progress:
                            progress(min(1, completed_us / total_us))
                        continue
                    window_position = len(selected_ranges)
                    frame_index = (start * fps + 999_999) // 1_000_000
                    reader.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
                    previous: Gray | None = None
                    previous_signature: Signature | None = None
                    previous_time = start
                    anchor: tuple[NDArray[np.float32], Gray] | None = None
                    static_start: int | None = None

                    while frame_index < frame_count:
                        if cancel and cancel.is_set():
                            raise Cancelled("Timelapse cleanup cancelled")
                        ok, frame = reader.read()
                        if not ok:
                            raise MediaError(
                                f"Cannot read selected timelapse footage: {source.path}"
                            )
                        frame_index += 1
                        time = round(reader.get(cv2.CAP_PROP_POS_MSEC) * 1000)
                        if time >= end:
                            break
                        if progress:
                            progress(min(1, (completed_us + max(0, time - start)) / total_us))
                        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.uint8)
                        view = appearance(gray)
                        current_signature: Signature | None = None
                        unchanged = anchor is not None and stable(anchor, view)
                        if unchanged:
                            if static_start is None:
                                static_start = previous_time
                        else:
                            if static_start is not None and time - static_start >= minimum_static:
                                selected_ranges.append(IdleRange(
                                    source_id=source.id, start_us=static_start, end_us=time,
                                    reason="timelapse_static_view",
                                ))
                            static_start = None
                            anchor = view
                            if previous is not None:
                                current_signature = signature(gray)
                                if previous_signature is None:
                                    previous_signature = signature(previous)
                                if camera_only(
                                    previous, gray, previous_signature, current_signature,
                                ):
                                    selected_ranges.append(IdleRange(
                                        source_id=source.id, start_us=max(start, previous_time),
                                        end_us=min(end, time), reason="timelapse_camera_only",
                                    ))
                        previous, previous_time = gray, time
                        previous_signature = current_signature
                    stop = min(end, previous_time + step)
                    if static_start is not None and stop - static_start >= minimum_static:
                        selected_ranges.append(IdleRange(
                            source_id=source.id, start_us=static_start, end_us=stop,
                            reason="timelapse_static_view",
                        ))
                    completed_us += end - start
                    if cancel and cancel.is_set():
                        raise Cancelled("Timelapse cleanup cancelled")
                    selected_ranges[window_position:] = conservative_camera_ranges(
                        selected_ranges[window_position:],
                    )
                    cache.put("timelapse_window", window_id, window_key, {
                        "ranges": [value.model_dump()
                                   for value in selected_ranges[window_position:]],
                    })
            finally:
                reader.release()
            if cancel and cancel.is_set():
                raise Cancelled("Timelapse cleanup cancelled")
            cache.put("timelapse_cleanup", source.id, key,
                      {"ranges": [value.model_dump() for value in selected_ranges]})
            ranges.extend(selected_ranges)
        if progress:
            progress(1)
        return ranges


def clean_timelapse(
    root: Path, project: Project, plan: RenderPlan, cancel: Event | None = None,
    progress: Callable[[float], None] | None = None,
) -> RenderPlan:
    ranges = cleanup_ranges(root, project, plan, cancel, progress)
    if not ranges:
        return plan
    retained_us = sum(d.source_end_us - d.source_start_us for d in plan.decisions
                      if d.action != "DROP")
    context_only = (
        all(r.reason == "timelapse_static_view" for r in ranges)
        and sum(r.end_us - r.start_us for r in ranges) >= retained_us
        and project.settings.timelapse_outcome_hold_seconds > 0
    )
    if context_only:
        # A completely static input can produce a brief original view, not a
        # requested-length movie filled with identical frames.
        requested_seconds = plan.actual_frame_count / plan.output_fps
        context_seconds = (
            requested_seconds
            if requested_seconds <= project.settings.timelapse_static_cut_seconds
            else project.settings.timelapse_outcome_hold_seconds
        )
        context_us = round(context_seconds * 1_000_000)
        first = ranges[0]
        ranges = [first.model_copy(update={
            "start_us": min(first.end_us, first.start_us + context_us),
        }), *ranges[1:]]
        ranges = [value for value in ranges if value.start_us < value.end_us]
    result = strip_screens(
        project, plan, ranges, provenance="story:timelapse_cleanup",
        warning="Confirmed camera-only movements and sustained static views removed",
    )
    if context_only:
        result = result.model_copy(update={"warnings": result.warnings + [
            "No visible work detected; only brief original context retained",
        ]})
    return result.model_copy(update={"settings": result.settings | {
        "timelapse_cleanup_camera_us": sum(r.end_us-r.start_us for r in ranges
                                            if r.reason == "timelapse_camera_only"),
        "timelapse_cleanup_static_us": sum(r.end_us-r.start_us for r in ranges
                                            if r.reason == "timelapse_static_view"),
    }})
