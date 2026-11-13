"""Choose a clean recorded result rather than freezing an arbitrary last episode."""

import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import cv2
import numpy as np
from numpy.typing import NDArray

from sculptcut.analysis import Gray
from sculptcut.bookends import FINAL_PROVENANCES, FINAL_ZOOM
from sculptcut.domain import Project, Roi
from sculptcut.media import Cancelled
from sculptcut.planning import RenderDecision, RenderPlan, validate_plan
from sculptcut.proxy import proxy_path
from sculptcut.roi import pixel_bounds
from sculptcut.screen_cleanup import screen_kind
from sculptcut.shorts import sculpt_footprint, viewport_box, viewport_shorts_camera
from sculptcut.shorts_quality import proxy_sample
from sculptcut.subject_camera import still_frame
from sculptcut.webcam import AnalysisCapture, webcam_roi


@dataclass(frozen=True)
class FinalResultSettings:
    search_seconds: int = 600
    candidates: int = 120
    minimum_height: float = 0.35
    edge_margin: float = 0.02
    menu_min_width: float = 0.05
    menu_min_height: float = 0.15
    menu_max_area: float = 0.5
    menu_fill: float = 0.7
    menu_text_rows: int = 8
    menu_gray_step: int = 16
    menu_levels: int = 8
    menu_close_size: int = 5
    menu_border: int = 3
    menu_edge_low: int = 15
    menu_edge_high: int = 40
    menu_row_fraction: float = 0.1
    verification_width: int = 960
    model_height_fraction: float = 0.76
    model_width_fraction: float = 0.84


SETTINGS = FinalResultSettings()
FINAL_RESULT_VERSION = 311


def presentation_crop(
    viewport: Roi, box: Roi, width: int, height: int, aspect: float,
) -> Roi | None:
    """Fit the whole detected result with margins, using only the clean viewport."""
    crop_h = max(box.height * height / SETTINGS.model_height_fraction,
                 box.width * width / (SETTINGS.model_width_fraction * aspect))
    if crop_h > min(viewport.height * height, viewport.width * width / aspect):
        return None  # Keep the recorded framing rather than cut off a wide result.
    h, w = crop_h / height, crop_h * aspect / width
    x = min(max(viewport.x, box.x + box.width / 2 - w / 2), viewport.x + viewport.width - w)
    y = min(max(viewport.y, box.y + box.height / 2 - h / 2), viewport.y + viewport.height - h)
    inset_x, inset_y = w * (1-1/(1+FINAL_ZOOM))/2, h * (1-1/(1+FINAL_ZOOM))/2
    if (box.x < x+inset_x or box.y < y+inset_y
            or box.x+box.width > x+w-inset_x or box.y+box.height > y+h-inset_y):
        return None
    return Roi(x=x, y=y, width=w, height=h)


def menu_covers_model(region: Gray) -> bool:
    """Find a filled rectangular popup with repeated rows of UI text."""
    edges = cv2.Canny(region, SETTINGS.menu_edge_low, SETTINGS.menu_edge_high)
    levels = region // SETTINGS.menu_gray_step
    for level in np.argsort(np.bincount(levels.ravel()))[-SETTINGS.menu_levels :]:
        mask = cv2.morphologyEx(
            (levels == level).astype(np.uint8),
            cv2.MORPH_CLOSE,
            np.ones((SETTINGS.menu_close_size, SETTINGS.menu_close_size), np.uint8),
        )
        _, _, stats, _ = cv2.connectedComponentsWithStats(mask)
        for x, y, w, h, area in stats[1:]:
            if not (
                w >= region.shape[1] * SETTINGS.menu_min_width
                and h >= region.shape[0] * SETTINGS.menu_min_height
                and w * h < region.size * SETTINGS.menu_max_area
                and area / (w * h) >= SETTINGS.menu_fill
            ):
                continue
            border = SETTINGS.menu_border
            rows = (
                np.count_nonzero(
                    edges[y + border : y + h - border, x + border : x + w - border],
                    axis=1,
                )
                > w * SETTINGS.menu_row_fraction
            )
            if (
                len(rows)
                and np.count_nonzero(rows & ~np.r_[False, rows[:-1]]) >= SETTINGS.menu_text_rows
            ):
                return True
    return False


def result_frame(frame: NDArray[np.uint8]) -> tuple[Roi, Roi] | None:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.uint8)
    if screen_kind(gray):
        return None
    viewport = viewport_box(gray)
    if viewport is None:
        return None
    x, y, r, b = pixel_bounds(viewport, gray.shape[1], gray.shape[0])
    if menu_covers_model(gray[y:b, x:r]):
        return None
    box = sculpt_footprint(gray, viewport)
    if box is None or box.height < viewport.height * SETTINGS.minimum_height:
        return None
    margin = SETTINGS.edge_margin
    if (
        box.x < viewport.x + viewport.width * margin
        or box.x + box.width > viewport.x + viewport.width * (1 - margin)
        or box.y < viewport.y + viewport.height * margin
    ):
        return None
    return viewport, box


def ensure_final_result(
    root: Path,
    project: Project,
    plan: RenderPlan,
    cancel: Event | None = None,
) -> RenderPlan:
    if project.settings.preset not in ("timelapse", "shorts", "music_shorts"):
        return plan
    if project.settings.final_view_seconds <= 0:
        return plan
    active = [i for i, d in enumerate(plan.decisions) if d.output_frame_count]
    final = []
    for i in reversed(active):
        if plan.decisions[i].provenance not in FINAL_PROVENANCES:
            break
        final.append(i)
    if not final:
        return plan
    source = project.sources[-1]
    frame_us = max(1, (1_000_000 * source.fps_den + source.fps_num - 1) // source.fps_num)
    excluded = [
        (o.start_us, o.end_us)
        for o in project.overrides
        if o.source_id == source.id and o.action == "DROP"
    ]
    excluded += [
        (d.source_start_us, d.source_end_us)
        for d in plan.decisions
        if d.source_id == source.id and d.provenance in ("user:shorts_manual_cut", "user:DROP")
    ]
    body_end = max(
        (
            d.source_end_us
            for i, d in enumerate(plan.decisions)
            if i not in final and d.output_frame_count and d.source_id == source.id
        ),
        default=0,
    )

    def allowed(time: int) -> bool:
        return time >= body_end and not any(a < time + frame_us and b > time for a, b in excluded)

    if plan.settings.get("final_result_version") == FINAL_RESULT_VERSION and all(
        plan.decisions[i].source_id == source.id
        and plan.decisions[i].action == "HOLD"
        and plan.decisions[i].source_start_us == plan.settings.get("final_result_source_us")
        and allowed(plan.decisions[i].source_start_us)
        for i in final
    ):
        return plan
    path = proxy_path(root, project, source)
    if not path.is_file():
        return plan
    reader = AnalysisCapture(path, webcam_roi(project.settings, source.id))
    selected = None
    try:
        if not reader.isOpened():
            return plan
        latest = min(
            source.duration_us - frame_us,
            round(
                (reader.get(cv2.CAP_PROP_FRAME_COUNT) - 1)
                * 1_000_000
                / reader.get(cv2.CAP_PROP_FPS)
            ),
        )
        earliest = max(body_end, latest - SETTINGS.search_seconds * 1_000_000)
        for timestamp in np.linspace(latest, earliest, SETTINGS.candidates):
            if cancel and cancel.is_set():
                raise Cancelled("Final result selection cancelled")
            time = round(timestamp)
            if not allowed(time):
                continue
            frame = proxy_sample(reader, time, reader.get(cv2.CAP_PROP_FPS), cancel)
            if frame is not None and result_frame(frame) is not None:
                # Confirm the exact original image that the HOLD renderer will
                # decode, without encoding any intermediate source video.
                with tempfile.TemporaryDirectory(prefix="final-frame-", dir=root) as scratch:
                    still = still_frame(
                        source,
                        time,
                        Path(scratch) / "frame.png",
                        cancel,
                        sample_width=SETTINGS.verification_width,
                    )
                    original = cv2.imdecode(np.fromfile(still, dtype=np.uint8), cv2.IMREAD_COLOR)
                view = result_frame(original.astype(np.uint8)) if original is not None else None
                if view is not None:
                    selected = time, view
                    break
    finally:
        reader.release()
    if selected is None:
        return plan
    time, (viewport, box) = selected
    end = time + frame_us
    count = sum(plan.decisions[i].output_frame_count for i in final)
    template = plan.decisions[final[0]]
    crop = template.crop
    camera = None
    if project.settings.preset == "timelapse":
        from sculptcut.rendering import export_size

        output_width, output_height = export_size(project, plan)
        width, height = source.width, source.height
        if source.rotation % 180 == 90:
            width, height = height, width
        crop = presentation_crop(viewport, box, width, height, output_width / output_height) or crop
    if project.settings.preset in ("shorts", "music_shorts"):
        from sculptcut.shorts_layout import uses_square

        width, height = source.width, source.height
        if source.rotation % 180 == 90:
            width, height = height, width
        if uses_square(project.settings):
            from sculptcut.cropping import fit_video_aspect
            from sculptcut.square_tracking import horizontal_camera, safe_viewport

            roi = fit_video_aspect(project.settings.shorts_square_rois[source.id], width, height, 1)
            camera = horizontal_camera(
                roi,
                [(time, box)],
                project.settings,
                time,
                viewport=safe_viewport([viewport], roi, [box]),
            )
            crop = roi
        else:
            aspect = (
                int(plan.settings.get("width", 1080))
                / int(plan.settings.get("height", 1920))
                * height
                / width
            )
            camera = viewport_shorts_camera(viewport, [box], aspect, time, 70)
            crop = viewport
    hold = RenderDecision(
        source_id=source.id,
        source_start_us=time,
        source_end_us=end,
        action="HOLD",
        output_frame_count=count,
        provenance="user:final_tail",
        crop=crop,
        subject_camera=camera,
    )
    decisions = []
    inserted = False
    for i, d in enumerate(plan.decisions):
        if i in final:
            d = d.model_copy(
                update={
                    "action": "DROP",
                    "output_frame_count": 0,
                    "provenance": "auto:replaced_final",
                    "sample_times_us": [],
                }
            )
        if d.source_id == source.id and d.source_start_us < end and d.source_end_us > time:
            if d.source_start_us < time:
                decisions.append(d.model_copy(update={"source_end_us": time}))
            if not inserted:
                decisions.append(hold)
                inserted = True
            if d.source_end_us > end:
                decisions.append(d.model_copy(update={"source_start_us": end}))
        else:
            decisions.append(d)
    result = plan.model_copy(
        update={
            "decisions": decisions,
            "settings": plan.settings
            | {
                "final_result_version": FINAL_RESULT_VERSION,
                "final_result_source_us": time,
                "bookends": True,
                "final_view_frames": count,
            },
        }
    )
    validate_plan(project, result)
    return result
