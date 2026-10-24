"""Chronological shorts highlights and conservative model-centred reframing."""

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import cv2
import numpy as np
from numpy.typing import NDArray

from sculptcut.analysis import Gray
from sculptcut.budget import distribute
from sculptcut.domain import CameraPoint, Project, Roi, SubjectCamera
from sculptcut.idle import IdleRange
from sculptcut.media import Cancelled
from sculptcut.planning import RenderDecision, RenderPlan, validate_plan
from sculptcut.proxy import proxy_path
from sculptcut.roi import pixel_bounds
from sculptcut.screen_cleanup import screen_kind, strip_screens
from sculptcut.segmentation import Segment
from sculptcut.subject_camera import tracking_clip
from sculptcut.webcam import AnalysisCapture, webcam_roi


@dataclass(frozen=True)
class CameraStability:
    outlier_window_seconds: float = 0.2
    outlier_distance: float = 0.04
    smoothing_window_seconds: float = 0.8
    stationary_range: float = 0.02
    envelope_percentile: float = 0.98
    scene_cut_change: float = 0.012
    minimum_shot_seconds: float = 1.5
    process_min_viewport_fraction: float = 0.35
    process_max_viewport_fraction: float = 0.55
    duplicate_min_width_fraction: float = 0.3
    duplicate_combined_width_fraction: float = 0.7
    duplicate_min_area_ratio: float = 0.5
    duplicate_max_overlap_fraction: float = 0.03


CAMERA_STABILITY = CameraStability()


def highlight_plan(
    project: Project,
    base: RenderPlan,
    segments: list[Segment],
    root: Path | None = None,
    cancel: Event | None = None,
) -> RenderPlan:
    fps = base.output_fps
    total = sum(s.duration_us for s in project.sources)
    target = max(1, min(base.target_frame_count, total * fps // 1_000_000))
    # Short recordings need no temporal omissions; longer sessions get about 20 shots.
    count = min(20, max(1, total // 5_000_000))
    selections: list[tuple[str, int, int]] = []
    final_selection: tuple[str, int, int] | None = None
    for index in range(count):
        left, right = total * index // count, total * (index + 1) // count
        candidates = []
        for source in project.sources:
            a = max(0, left - source.global_start_us)
            b = min(source.duration_us, right - source.global_start_us)
            if b > a:
                candidates.append((source, a, b))
        source, a, b = max(candidates, key=lambda item: item[2] - item[1])
        length = min(b - a, 5_000_000)
        if root is not None and project.settings.sculpt_only and index == count - 1:
            from sculptcut.sculpt_only import final_start

            selected = final_start(root, project, source, length, cancel, a)
            if selected is None:
                continue
            start = selected
            length = min(length, source.duration_us - start)
            final_selection = (source.id, start, start + length)
        elif root is not None and index < count - 1 and (index > 0 or project.settings.sculpt_only):
            from sculptcut.shorts_selection import choose_start

            selected = choose_start(root, project, source, a, b, length, segments, cancel)
            if selected is None:
                continue
            start = selected
            if project.settings.sculpt_only:
                from sculptcut.sculpt_only import SETTINGS

                left_edge = max(a, start - SETTINGS.context_us)
                right_edge = min(b, start + length + SETTINGS.context_us)
                for override in project.overrides:
                    if override.source_id != source.id or override.action != "DROP":
                        continue
                    if override.end_us <= start:
                        left_edge = max(left_edge, override.end_us)
                    elif override.start_us >= start + length:
                        right_edge = min(right_edge, override.start_us)
                start, length = left_edge, right_edge - left_edge
        elif index == 0:
            start = a
        elif index == count - 1:
            start = b - length
        else:
            relevant = [
                s
                for s in segments
                if s.source_id == source.id
                and s.start_us < b
                and s.end_us > a
                and s.kind not in ("STATIC", "PROCESSING", "INTERFACE", "CAMERA")
            ]
            best = max(
                relevant,
                key=lambda s: (
                    s.components.get("observed_change", 0) + s.components.get("novelty", 0)
                ),
                default=None,
            )
            center = (max(a, best.start_us) + min(b, best.end_us)) // 2 if best else (a + b) // 2
            start = max(a, min(b - length, center - length // 2))
        selections.append((source.id, start, start + length))
    if root is None and sum(end - start for _, start, end in selections) < min(
        total, base.target_frame_count * 1_000_000 // fps
    ):
        selections = [(source.id, 0, source.duration_us) for source in project.sources]
    if not selections:
        raise ValueError("No sculpting footage found in strict selection")
    decisions = []
    caps, preferences, minima = [], [], []
    for source in project.sources:
        position = 0
        for sid, start, end in selections:
            if sid != source.id:
                continue
            if start > position:
                decisions.append(
                    RenderDecision(
                        source_id=sid,
                        source_start_us=position,
                        source_end_us=start,
                        action="DROP",
                        output_frame_count=0,
                        provenance="user:shorts_summary_omission",
                    )
                )
                caps.append(0)
                minima.append(0)
                preferences.append(1.0)
            cap = max(1, (end - start) * fps // 1_000_000)
            final = (sid, start, end) == final_selection or (
                not project.settings.sculpt_only
                and sid == project.sources[-1].id
                and end == project.sources[-1].duration_us
            )
            low = min(cap, 5 * fps, target // 5) if final else 1
            decisions.append(
                RenderDecision(
                    source_id=sid,
                    source_start_us=start,
                    source_end_us=end,
                    action="SPEED",
                    output_frame_count=low,
                    provenance="user:shorts_final" if final else "user:shorts_highlight",
                )
            )
            caps.append(cap)
            minima.append(low)
            preferences.append(
                float(cap * (2 if final else 0.7 if len(preferences) % 3 == 0 else 1))
            )
            position = end
        if position < source.duration_us:
            decisions.append(
                RenderDecision(
                    source_id=source.id,
                    source_start_us=position,
                    source_end_us=source.duration_us,
                    action="DROP",
                    output_frame_count=0,
                    provenance="user:shorts_summary_omission",
                )
            )
            caps.append(0)
            minima.append(0)
            preferences.append(1.0)
    actual = min(target, sum(caps))
    counts = distribute(minima, preferences, caps, actual)
    result = base.model_copy(
        update={
            "decisions": [
                d.model_copy(update={"output_frame_count": n})
                for d, n in zip(decisions, counts, strict=True)
            ],
            "target_frame_count": base.target_frame_count,
            "actual_frame_count": actual,
            "settings": base.settings | {"preset": "shorts", "width": 1080, "height": 1920},
            "warnings": [
                "Shorts selects highlights; omitted work is not classified as idle or Undo"
            ]
            + (
                ["Insufficient selected footage for the requested duration without slowing"]
                if actual < base.target_frame_count
                else []
            ),
        }
    )
    validate_plan(project, result)
    return result


def canvas_bounds(gray: Gray) -> tuple[int, int, int, int, float] | None:
    """Detect the connected canvas background independently of its subject."""
    height, width = gray.shape
    central = gray[height // 8 : height * 7 // 8, width // 8 : width * 7 // 8]
    mode = int(np.bincount((central // 4).ravel(), minlength=64).argmax())
    background = float(np.median(central[central // 4 == mode]))
    flat = (np.abs(gray.astype(np.float32) - background) <= 4).astype(np.uint8)
    count, _, stats, _ = cv2.connectedComponentsWithStats(flat)
    if count < 2:
        return None
    largest = int(stats[1:, cv2.CC_STAT_AREA].max())
    components = stats[1:]
    parts = components[
        components[:, cv2.CC_STAT_AREA] >= max(largest * 0.15, width * height * 0.015)
    ]
    if not len(parts):
        return None
    # A large model divides the canvas into disconnected left/right regions.
    # They share its background colour and must form one viewport.
    x, y = int(parts[:, 0].min()), int(parts[:, 1].min())
    w = int((parts[:, 0] + parts[:, 2]).max()) - x
    h = int((parts[:, 1] + parts[:, 3]).max()) - y
    area = int(parts[:, 4].sum())
    if area < width * height * 0.2:
        return None
    return x, y, w, h, background


def viewport_box(gray: Gray) -> Roi | None:
    bounds = canvas_bounds(gray)
    if bounds is None:
        return None
    x, y, w, h, _ = bounds
    height, width = gray.shape
    return Roi(x=x / width, y=y / height, width=w / width, height=h / height)


def model_box(gray: Gray, viewport: Roi | None = None, previous: Roi | None = None) -> Roi | None:
    """Find the dominant foreground object inside the detected canvas."""
    height, width = gray.shape
    if viewport is None:
        bounds = canvas_bounds(gray)
        if bounds is None:
            return None
        x, y, w, h, background = bounds
    else:
        x, y, r, b = pixel_bounds(viewport, width, height)
        w, h = r - x, b - y
        pixels = gray[y:b, x:r]
        mode = int(np.bincount((pixels // 4).ravel(), minlength=64).argmax())
        background = float(np.median(pixels[pixels // 4 == mode]))
    region = gray[y : y + h, x : x + w]
    mask = (np.abs(region.astype(np.float32) - background) > 14).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), dtype=np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = [
        c
        for c in contours
        if not (
            cv2.contourArea(c) < w * h * 0.02
            and (cv2.boundingRect(c)[0] < w * 0.12 or cv2.boundingRect(c)[0] > w * 0.85)
            and cv2.boundingRect(c)[1] < h * 0.22
        )
    ]
    contours = sorted(contours, key=cv2.contourArea, reverse=True)
    if not contours or not 0.002 <= cv2.contourArea(contours[0]) / (w * h) <= 0.9:
        return None
    significant = [
        c.astype(np.int32)
        for c in contours
        if cv2.contourArea(c) >= cv2.contourArea(contours[0]) * 0.2
    ]

    def subject_score(contour: NDArray[np.int32]) -> float:
        bx, by, bw, bh = cv2.boundingRect(contour)
        area = float(cv2.contourArea(contour))
        detail = float(cv2.Laplacian(region[by : by + bh, bx : bx + bw], cv2.CV_32F).var())
        completeness = 0.65 if bx <= 1 or by <= 1 or bx + bw >= w - 1 or by + bh >= h - 1 else 1.0
        score = area * completeness * (1 + min(detail / 1000, 0.5))
        if previous is not None:
            cx, cy = (x + bx + bw / 2) / width, (y + by + bh / 2) / height
            distance = (
                (cx - previous.x - previous.width / 2) ** 2
                + (cy - previous.y - previous.height / 2) ** 2
            ) ** 0.5
            score *= 1 + 3 * max(0, 1 - distance / 0.2)
        return score

    bx, by, bw, bh = cv2.boundingRect(max(significant, key=subject_score))
    # A zoomed source can already clip its subject at the canvas edge. Preserve
    # every visible part instead of requiring an imaginary uncropped model.
    left, top = max(0, x + bx - bw * 0.12), max(0, y + by - bh * 0.12)
    right, bottom = min(width, x + bx + bw * 1.12), min(height, y + by + bh * 1.12)
    nx, ny = left / width, top / height
    return Roi(
        x=nx,
        y=ny,
        width=min(1 - nx, (right - left) / width),
        height=min(1 - ny, (bottom - top) / height),
    )


def sculpt_footprint(gray: Gray, viewport: Roi, previous: Roi | None = None) -> Roi | None:
    """Bound one visible sculpt, keeping its side when two full models are shown."""
    height, width = gray.shape
    x, y, right, bottom = pixel_bounds(viewport, width, height)
    region = gray[y:bottom, x:right]
    mode = int(np.bincount((region // 4).ravel(), minlength=64).argmax())
    background = float(np.median(region[region // 4 == mode]))
    mask = (np.abs(region.astype(np.float32) - background) > 14).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), dtype=np.uint8))
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    if count < 2:
        return None
    largest = int(stats[1:, cv2.CC_STAT_AREA].max())
    viewport_width, viewport_height = right - x, bottom - y
    parts = []
    for part in stats[1:]:
        px, py, pw, ph, area = (int(value) for value in part)
        if area < max(largest * 0.08, viewport_width * viewport_height * 0.002):
            continue
        corner_widget = (
            py < viewport_height * 0.12
            and (px < viewport_width * 0.12 or px + pw > viewport_width * 0.88)
            and area < largest * 0.5
        )
        if not corner_widget:
            parts.append((px, py, pw, ph, area))
    if not parts:
        return None
    largest_parts = sorted(parts, key=lambda part: part[4], reverse=True)
    if len(largest_parts) >= 2:
        first, second = sorted(largest_parts[:2], key=lambda part: part[0])
        overlap = first[0] + first[2] - second[0]
        combined_width = second[0] + second[2] - first[0]
        separate_models = (
            min(first[4], second[4])
            >= max(first[4], second[4]) * CAMERA_STABILITY.duplicate_min_area_ratio
            and min(first[2], second[2])
            >= viewport_width * CAMERA_STABILITY.duplicate_min_width_fraction
            and combined_width
            >= viewport_width * CAMERA_STABILITY.duplicate_combined_width_fraction
            and overlap <= viewport_width * CAMERA_STABILITY.duplicate_max_overlap_fraction
        )
        if separate_models:
            if previous is None:
                chosen = max((first, second), key=lambda part: part[4])
            else:
                previous_x = previous.x + previous.width / 2
                chosen = min(
                    (first, second),
                    key=lambda part: abs((x + part[0] + part[2] / 2) / width - previous_x),
                )
            parts = [chosen]
    left = min(part[0] for part in parts)
    top = min(part[1] for part in parts)
    far_right = max(part[0] + part[2] for part in parts)
    far_bottom = max(part[1] + part[3] for part in parts)
    return Roi(
        x=(x + left) / width,
        y=(y + top) / height,
        width=(far_right - left) / width,
        height=(far_bottom - top) / height,
    )


def portrait_box(box: Roi, width: int, height: int, wide: bool) -> Roi:
    factor = 1.35 if wide else 1.05
    w, h = box.width * factor, box.height * factor
    ratio = (9 / 16) * height / width
    w, h = max(w, h * ratio), max(h, w / ratio)
    if w > 1 or h > 1:
        # The model is too wide to fill 9:16 safely; retain it with letterboxing.
        w, h = min(1, box.width * factor), min(1, box.height * factor)
    x = min(1 - w, max(0, box.x + box.width / 2 - w / 2))
    y = min(1 - h, max(0, box.y + box.height / 2 - h / 2))
    return Roi(x=x, y=y, width=w, height=h)


def tracked_subjects(
    samples: list[tuple[int, Roi | None]], viewport: Roi, full_frame_fallback: bool
) -> list[tuple[int, Roi]]:
    """Hold the detected model through brief misses instead of jumping to the canvas."""
    first = next((box for _, box in samples if box is not None), None)
    if full_frame_fallback or first is None:
        return [(time_us, viewport) for time_us, _ in samples]
    current = first
    tracked = []
    for time_us, box in samples:
        if box is not None:
            current = box
        tracked.append((time_us, current))
    return tracked


def stabilized_points(
    tracked: list[tuple[int, Roi]], fps: int, settings: CameraStability = CAMERA_STABILITY
) -> list[CameraPoint]:
    """Follow sustained model movement without copying detection jitter to the crop."""
    centers = np.array(
        [[box.x + box.width / 2, box.y + box.height / 2] for _, box in tracked],
        dtype=np.float64,
    )
    if len(centers) > 2:
        outlier_radius = max(1, round(fps * settings.outlier_window_seconds / 2))
        robust = centers.copy()
        for index in range(len(centers)):
            median = np.median(
                centers[max(0, index - outlier_radius) : index + outlier_radius + 1],
                axis=0,
            )
            if np.linalg.norm(centers[index] - median) > settings.outlier_distance:
                robust[index] = median
        radius = max(1, round(fps * settings.smoothing_window_seconds / 2))
        positions = np.arange(len(centers))
        smooth = robust.copy()
        for index in range(len(centers)):
            lo, hi = max(0, index - radius), min(len(centers), index + radius + 1)
            offsets = positions[lo:hi] - index
            design = np.column_stack([np.ones(hi - lo), offsets])
            smooth[index] = np.linalg.lstsq(design, robust[lo:hi], rcond=None)[0][0]
        for axis in (0, 1):
            if float(np.ptp(smooth[:, axis])) <= settings.stationary_range:
                smooth[:, axis] = np.median(smooth[:, axis])
        centers = smooth
    return [
        CameraPoint(
            time_us=time_us,
            x=float(np.clip(centers[index, 0], 0, 1)),
            y=float(np.clip(centers[index, 1], 0, 1)),
        )
        for index, (time_us, _) in enumerate(tracked)
    ]


def subject_extent(tracked: list[tuple[int, Roi]], percentile: float) -> tuple[float, float]:
    """Use a typical large model size without letting brief false bounds set the zoom."""
    return (
        float(np.quantile([box.width for _, box in tracked], percentile)),
        float(np.quantile([box.height for _, box in tracked], percentile)),
    )


def locked_shorts_camera(
    tracked: list[tuple[int, Roi]],
    measured: list[Roi],
    viewport: Roi,
    fps: int,
    aspect: float,
    margin: float,
    background: int,
    width_bounds: tuple[float, float] | None = None,
) -> SubjectCamera:
    """Choose one camera for the shot that contains the model's observed travel."""
    centers = stabilized_points(tracked, fps)
    cx = float(np.median([point.x for point in centers]))
    cy = float(np.median([point.y for point in centers]))
    boxes = measured or [box for _, box in tracked]
    half_width = float(
        np.quantile(
            [max(cx - box.x, box.x + box.width - cx) for box in boxes],
            CAMERA_STABILITY.envelope_percentile,
        )
    )
    half_height = float(
        np.quantile(
            [max(cy - box.y, box.y + box.height - cy) for box in boxes],
            CAMERA_STABILITY.envelope_percentile,
        )
    )
    camera_w = max(2 * half_width * margin, 2 * half_height * margin * aspect)
    if width_bounds is not None:
        # Fast source zooms should remain visible as source motion; fitting the
        # entire zoom range into one shot makes the sculpt too small to inspect.
        camera_w = float(np.clip(camera_w, *width_bounds))
    camera_h = camera_w / aspect
    return SubjectCamera(
        viewport=viewport,
        fit_subject=True,
        blur_background=False,
        width=camera_w,
        height=camera_h,
        points=[CameraPoint(time_us=tracked[0][0], x=cx, y=cy)],
        background=background,
    )


def viewport_shorts_camera(
    viewport: Roi,
    measured: list[Roi],
    aspect: float,
    time_us: int,
    background: int,
    side_margin: float = 0.04,
    ignore_isolated_outliers: bool = False,
) -> SubjectCamera:
    """Anchor ordinary Shorts to the canvas, moving sideways only when needed."""
    width = min(viewport.width, viewport.height * aspect)
    if measured:
        if ignore_isolated_outliers:
            # A ZBrush popup can become the largest foreground component for
            # a couple of frames. Trim those isolated detections so they do
            # not widen the ordinary Shorts camera to the entire canvas.
            subject_left = float(np.quantile([box.x for box in measured], 0.1))
            subject_right = float(np.quantile([box.x + box.width for box in measured], 0.9))
        else:
            subject_left = min(box.x for box in measured)
            subject_right = max(box.x + box.width for box in measured)
        spread = max(0.0, subject_right - subject_left)
        width = min(viewport.width, max(width, spread + 2 * side_margin))
    # Extra horizontal travel is fitted by adding canvas-colored padding above
    # and below. Source pixels still come exclusively from camera.viewport.
    height = viewport.height
    left = viewport.x + width / 2
    right = viewport.x + viewport.width - width / 2
    center = viewport.x + viewport.width / 2
    if measured:
        needed_left = subject_right + side_margin - width / 2
        needed_right = subject_left - side_margin + width / 2
        legal_left, legal_right = viewport.x + width / 2, viewport.x + viewport.width - width / 2
        left = max(left, needed_left)
        right = min(right, needed_right)
        if left > right:
            # Near a canvas edge the requested safety margin cannot fit.
            # Press the crop against that edge instead of resetting to the
            # middle of the viewport and cutting the model in half.
            center = (subject_left + subject_right) / 2
            left, right = legal_left, legal_right
    center = float(np.clip(center, left, right))
    return SubjectCamera(
        viewport=viewport,
        fit_subject=True,
        width=width,
        height=height,
        points=[CameraPoint(time_us=time_us, x=center, y=viewport.y + viewport.height / 2)],
        background=background,
    )


def scene_cut_frames(
    changes: list[tuple[int, float]],
    frame_count: int,
    fps: int,
    settings: CameraStability = CAMERA_STABILITY,
) -> list[int]:
    """Use short-form cuts at large source changes instead of moving the camera."""
    minimum = max(1, round(fps * settings.minimum_shot_seconds))
    chosen: list[int] = []
    for frame, change in sorted(changes, key=lambda item: (-item[1], item[0])):
        if change < settings.scene_cut_change:
            break
        if frame < minimum or frame_count - frame < minimum:
            continue
        if all(abs(frame - prior) >= minimum for prior in chosen):
            chosen.append(frame)
    return sorted(chosen)


def reframe_shorts(
    root: Path,
    project: Project,
    plan: RenderPlan,
    cancel: Event | None,
    progress: Callable[[float], None] | None = None,
    *,
    preserve_selection: bool = False,
) -> RenderPlan:
    # Music synchronization renames the process story after initial framing.
    # Restored episodes still need the same viewport camera, not a camera
    # widened to contain every model position across their whole source span.
    process = plan.settings.get("story") in ("process", "music_episodes")
    if not process and not preserve_selection:
        plan = replace_black_shots(root, project, plan, cancel)
    decisions = plan.decisions[:]
    replacements: dict[int, list[RenderDecision]] = {}
    warnings = plan.warnings[:]
    screens: list[IdleRange] = []
    shot_index = 0
    shot_count = sum(d.action != "DROP" for d in plan.decisions)
    for source in project.sources:
        source_width, source_height = source.width, source.height
        if source.rotation % 180 == 90:
            source_width, source_height = source_height, source_width
        output_aspect = int(plan.settings["width"]) / int(plan.settings["height"])
        native_portrait = abs(source_width / source_height - output_aspect) <= output_aspect * 0.02
        source_viewport: Roi | None = None
        # Track the selected original frames: a 5 fps proxy misses fast pans/zooms.
        scratch = tempfile.TemporaryDirectory(prefix="sculptcut-tracking-")
        capture: cv2.VideoCapture | AnalysisCapture = cv2.VideoCapture()
        try:
            for index, decision in enumerate(decisions):
                if decision.source_id != source.id or decision.action == "DROP":
                    continue
                context_viewport: Roi | None = None
                if preserve_selection and process:
                    context = [d for d in decisions if d.source_id == source.id
                               and d.subject_camera is not None and d.provenance not in (
                                   "user:KEEP", "user:RETAIN", "user:SET_SPEED",
                                   "user:FORCE_MONTAGE",
                               )]
                    closest = min(context, key=lambda d: abs(
                        d.source_start_us - decision.source_start_us,
                    ), default=None)
                    if closest is not None and closest.subject_camera is not None:
                        context_viewport = closest.subject_camera.viewport
                        source_viewport = context_viewport
                wide = not process and shot_index % 2 == 0
                shot_index += 1
                if progress:
                    progress((shot_index - 1) / max(1, shot_count))
                if native_portrait:
                    # The source already fills Shorts. Avoid ZBrush-only
                    # screen/model detectors on native portrait footage.
                    full_viewport = Roi(x=0, y=0, width=1, height=1)
                    camera = SubjectCamera(
                        viewport=full_viewport,
                        width=1,
                        height=1,
                        points=[CameraPoint(time_us=decision.source_start_us, x=0.5, y=0.5)],
                        background=70,
                    )
                    decisions[index] = decision.model_copy(
                        update={"crop": full_viewport, "subject_camera": camera}
                    )
                    continue
                capture.release()
                ratio = (
                    (
                        decision.output_frame_count
                        / plan.output_fps
                        / ((decision.source_end_us - decision.source_start_us) / 1e6)
                    )
                    if process
                    else 1.0
                )
                clip = tracking_clip(
                    source,
                    decision.source_start_us,
                    decision.source_end_us,
                    Path(scratch.name) / "shot.mp4",
                    cancel,
                    output_frames=decision.output_frame_count if process else None,
                    output_fps=plan.output_fps,
                )
                capture = AnalysisCapture(clip, webcam_roi(project.settings, source.id))
                boxes = []
                viewports = []
                samples: list[tuple[int, Roi | None]] = []
                selected_subject: Roi | None = None
                backgrounds = []
                confident = True
                full_frame_fallback = False
                width, height = 0, 0
                while True:
                    if cancel and cancel.is_set():
                        raise Cancelled("Shorts framing cancelled")
                    ok, frame = capture.read()
                    if not ok:
                        break
                    timestamp = decision.source_start_us + round(
                        capture.get(cv2.CAP_PROP_POS_MSEC) * 1000 / ratio
                    )
                    if timestamp < decision.source_start_us:
                        continue
                    if timestamp >= decision.source_end_us:
                        break
                    height, width = frame.shape[:2]
                    frame = cv2.resize(
                        frame, (min(width, 960), round(height * min(width, 960) / width))
                    )
                    height, width = frame.shape[:2]
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.uint8)
                    kind = screen_kind(gray)
                    if kind:
                        screens.append(
                            IdleRange(
                                source_id=source.id,
                                start_us=max(
                                    decision.source_start_us,
                                    timestamp - round(1e6 / plan.output_fps / ratio),
                                ),
                                end_us=min(
                                    decision.source_end_us,
                                    timestamp + round(1e6 / plan.output_fps / ratio),
                                ),
                                reason=kind,
                            )
                        )
                        continue
                    bounds = canvas_bounds(gray)
                    if bounds:
                        backgrounds.append(bounds[-1])
                    # Close-up models can cover half the flat canvas, making
                    # background detection shrink it. Restored clips share the
                    # viewport already verified for nearby original episodes.
                    viewport = context_viewport or viewport_box(gray)
                    if viewport is None:
                        confident = False
                    else:
                        viewports.append(viewport)
                    box = (
                        sculpt_footprint(gray, source_viewport or viewport, selected_subject)
                        if viewport
                        else None
                    )
                    if not samples or timestamp > samples[-1][0]:
                        samples.append((timestamp, box))
                    if box is None:
                        confident = False
                    else:
                        boxes.append(box)
                        selected_subject = box
                if len(viewports) < 3:
                    if not samples:
                        continue
                    if source_viewport is None and viewports:
                        source_viewport = viewports[0]
                    if source_viewport is None:
                        full_frame_fallback = True
                        viewports = [Roi(x=0, y=0, width=1, height=1)]
                        warnings.append(
                            "Shorts kept an unrecognized application as a full frame: "
                            f"{source.id}@{decision.source_start_us}"
                        )
                    else:
                        viewports = [source_viewport]
                        warnings.append(
                            f"Shorts reused viewport: {source.id}@{decision.source_start_us}"
                        )
                # A foreground crossing the background can split its component;
                # that must not shrink the viewport and cut the subject itself.
                left = float(np.median([v.x for v in viewports]))
                top = float(np.median([v.y for v in viewports]))
                right = float(np.median([v.x + v.width for v in viewports]))
                bottom = float(np.median([v.y + v.height for v in viewports]))
                if right <= left or bottom <= top:
                    raise ValueError("ZBrush viewport moved too far during a shorts shot")
                viewport = Roi(x=left, y=top, width=right - left, height=bottom - top)
                if source_viewport and (
                    abs(viewport.x - source_viewport.x) < 0.025
                    and abs(viewport.y - source_viewport.y) < 0.025
                    and abs(viewport.width - source_viewport.width) < 0.05
                    and abs(viewport.height - source_viewport.height) < 0.05
                ):
                    viewport = source_viewport
                if not full_frame_fallback:
                    source_viewport = viewport
                if not confident or len(boxes) < 3:
                    warnings.append(
                        f"Shorts viewport fallback: {source.id}@{decision.source_start_us}"
                    )
                aspect = (9 / 16) * height / width
                if process:
                    margin = project.settings.shorts_tracking_margin
                else:
                    margin = (
                        max(
                            project.settings.shorts_tracking_margin,
                            1 / project.settings.shorts_subject_fill,
                        )
                        / project.settings.shorts_horizontal_fill
                        * (1.12 if wide else 1)
                    )
                tracked = tracked_subjects(samples, viewport, full_frame_fallback)
                boundaries = [0]
                boundaries.append(decision.output_frame_count)
                pieces = []
                for first_frame, last_frame in zip(boundaries, boundaries[1:], strict=False):
                    start = decision.source_start_us + round(
                        (decision.source_end_us - decision.source_start_us)
                        * first_frame
                        / decision.output_frame_count
                    )
                    end = decision.source_start_us + round(
                        (decision.source_end_us - decision.source_start_us)
                        * last_frame
                        / decision.output_frame_count
                    )
                    within = [(time_us, box) for time_us, box in tracked if start <= time_us < end]
                    if not within:
                        within = [(start, viewport)]
                    measured = [
                        box
                        for time_us, box in samples
                        if box is not None and start <= time_us < end
                    ]
                    background = round(float(np.median(backgrounds))) if backgrounds else 70
                    if full_frame_fallback:
                        camera = SubjectCamera(
                            viewport=viewport,
                            fit_subject=True,
                            width=viewport.width,
                            height=viewport.width / aspect,
                            points=[
                                CameraPoint(
                                    time_us=start,
                                    x=viewport.x + viewport.width / 2,
                                    y=viewport.y + viewport.height / 2,
                                )
                            ],
                            background=background,
                        )
                    elif process:
                        camera = viewport_shorts_camera(
                            viewport,
                            measured,
                            aspect,
                            start,
                            background,
                            ignore_isolated_outliers=process,
                        )
                    else:
                        camera = locked_shorts_camera(
                            within,
                            measured,
                            viewport,
                            plan.output_fps,
                            aspect,
                            margin,
                            background,
                        )
                    pieces.append(
                        decision.model_copy(
                            update={
                                "source_start_us": start,
                                "source_end_us": end,
                                "output_frame_count": last_frame - first_frame,
                                "crop": viewport,
                                "subject_camera": camera,
                            }
                        )
                    )
                replacements[index] = pieces
        finally:
            capture.release()
            scratch.cleanup()
    result = plan.model_copy(
        update={
            "decisions": [
                piece
                for index, decision in enumerate(decisions)
                for piece in replacements.get(index, [decision])
            ],
            "warnings": warnings,
        }
    )
    if progress:
        progress(1.0)
    if process:
        from sculptcut.process_story import STORY

        screens = sustained_screens(screens, STORY.screen_drop_us)
    return result if preserve_selection else strip_screens(project, result, screens)


def sustained_screens(ranges: list[IdleRange], minimum_us: int) -> list[IdleRange]:
    """Leave brief flashes to the final frame guard instead of splitting a shot."""
    merged: list[IdleRange] = []
    for current in sorted(ranges, key=lambda item: (item.source_id, item.start_us)):
        if (
            merged
            and merged[-1].source_id == current.source_id
            and merged[-1].end_us >= current.start_us
        ):
            merged[-1] = merged[-1].model_copy(
                update={"end_us": max(merged[-1].end_us, current.end_us)}
            )
        else:
            merged.append(current)
    return [item for item in merged if item.end_us - item.start_us >= minimum_us]


def replace_black_shots(
    root: Path, project: Project, plan: RenderPlan, cancel: Event | None
) -> RenderPlan:
    """Replace recording blackouts using omitted time, preserving shot budgets."""
    decisions = plan.decisions[:]
    warnings = plan.warnings[:]
    for source in project.sources:
        capture = AnalysisCapture(
            proxy_path(root, project, source), webcam_roi(project.settings, source.id)
        )
        try:

            def usable(
                start: int, length: int, reader: cv2.VideoCapture | AnalysisCapture = capture,
            ) -> bool:
                for fraction in (0.1, 0.5, 0.9):
                    if cancel and cancel.is_set():
                        raise Cancelled("Shorts framing cancelled")
                    reader.set(cv2.CAP_PROP_POS_MSEC, (start + length * fraction) / 1000)
                    ok, frame = reader.read()
                    if not ok:
                        return False
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    h, w = gray.shape
                    if np.mean(gray[h // 8 : h * 7 // 8, w // 8 : w * 7 // 8] > 16) < 0.05:
                        return False
                return True

            kept = [d for d in decisions if d.source_id == source.id and d.action != "DROP"]
            relocated: list[RenderDecision] = []
            for i, decision in enumerate(kept):
                length = decision.source_end_us - decision.source_start_us
                start = decision.source_start_us
                if not usable(start, length):
                    low = relocated[-1].source_end_us if relocated else 0
                    high = (
                        kept[i + 1].source_start_us if i + 1 < len(kept) else source.duration_us
                    ) - length
                    candidates = sorted(
                        set(range(low, high + 1, 30_000_000)) | {low, high},
                        key=lambda t: abs(t - start),
                    )
                    replacement = next((t for t in candidates if usable(t, length)), None)
                    if replacement is None:
                        raise ValueError("No visible recording near selected shorts fragment")
                    warnings.append(f"Shorts replaced black recording: {source.id}@{start}")
                    decision = decision.model_copy(
                        update={
                            "source_start_us": replacement,
                            "source_end_us": replacement + length,
                        }
                    )
                relocated.append(decision)
            rebuilt = []
            position = 0
            for decision in relocated:
                if decision.source_start_us > position:
                    rebuilt.append(
                        RenderDecision(
                            source_id=source.id,
                            source_start_us=position,
                            source_end_us=decision.source_start_us,
                            action="DROP",
                            output_frame_count=0,
                            provenance="user:shorts_summary_omission",
                        )
                    )
                rebuilt.append(decision)
                position = decision.source_end_us
            if position < source.duration_us:
                rebuilt.append(
                    RenderDecision(
                        source_id=source.id,
                        source_start_us=position,
                        source_end_us=source.duration_us,
                        action="DROP",
                        output_frame_count=0,
                        provenance="user:shorts_summary_omission",
                    )
                )
            first = next(i for i, d in enumerate(decisions) if d.source_id == source.id)
            decisions[first : first + sum(d.source_id == source.id for d in decisions)] = rebuilt
        finally:
            capture.release()
    result = plan.model_copy(update={"decisions": decisions, "warnings": warnings})
    validate_plan(project, result)
    return result
