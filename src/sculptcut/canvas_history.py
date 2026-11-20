"""A bounded, chronological canvas assembled from registered original-source views."""

from bisect import bisect_left
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

import cv2
import numpy as np
from numpy.typing import NDArray

from sculptcut.canvas_edges import trim_horizontal_chrome
from sculptcut.domain import ProjectSettings, SourceMedia, SubjectCamera
from sculptcut.media import Cancelled, MediaError
from sculptcut.roi import pixel_bounds
from sculptcut.subject_camera import center_at, size_at

if TYPE_CHECKING:
    from sculptcut.planning import RenderDecision

VERSION = 377
Bounds = tuple[int, int, int, int]


@dataclass(frozen=True)
class CanvasSettings:
    border_inset_pixels: int = 2
    source_border_fraction: float = .005
    registered_source_inset_fraction: float = .010
    feather_pixels: int = 6
    alignment_side: int = 720
    alignment_corners: int = 200
    alignment_quality: float = .03
    alignment_corner_distance: int = 4
    alignment_reprojection_pixels: float = .8
    alignment_snap_pixels: float = .25
    alignment_minimum_points: int = 10
    alignment_inlier_fraction: float = .6
    alignment_flow_error: float = .6
    alignment_maximum_shift_fraction: float = .08
    alignment_maximum_scale_error: float = .02
    alignment_scale_snap_ratio: float = .005
    alignment_maximum_rotation_degrees: float = 1
    alignment_minimum_spread_fraction: float = .1
    alignment_reference_limit: int = 4
    alignment_reference_interval: int = 90
    alignment_legacy_shift_fraction: float = .02
    alignment_legacy_snap_pixels: float = .5
    margin_background_distance: float = 40
    margin_background_chroma: float = 20


def _patch_transform(frame: NDArray[np.uint8], canvas: NDArray[np.uint8],
                     known: NDArray[np.bool_], bounds: Bounds,
                     *, refined: bool = True) -> NDArray[np.float64] | None:
    """Remove residual pan/scale using distributed, unchanged visible details.

    Forward/backward flow and a similarity consensus reject new paint/cursors.
    Large zooms and matches confined to a cursor/new brush stroke are rejected.
    """
    h, w = frame.shape[:2]
    scale = min(1.0, SETTINGS.alignment_side/max(h, w))
    size = (round(w*scale), round(h*scale))
    mask = np.zeros((h, w), np.uint8)
    x, y, r, b = bounds
    mask[y:b, x:r] = known[y:b, x:r]*255
    small_mask = cv2.resize(mask, size, interpolation=cv2.INTER_NEAREST)
    old = cv2.resize(cv2.cvtColor(canvas, cv2.COLOR_BGR2GRAY), size)
    new = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), size)
    points = cv2.goodFeaturesToTrack(
        old, SETTINGS.alignment_corners, SETTINGS.alignment_quality,
        SETTINGS.alignment_corner_distance, mask=small_mask)
    if points is None or len(points) < SETTINGS.alignment_minimum_points:
        return None
    forward, status, _ = cv2.calcOpticalFlowPyrLK(old, new, points, points.copy())
    if forward is None or status is None:
        return None
    backward, back_status, _ = cv2.calcOpticalFlowPyrLK(new, old, forward, forward.copy())
    if backward is None or back_status is None:
        return None
    valid = ((status.ravel() > 0) & (back_status.ravel() > 0)
             & (np.linalg.norm((points-backward).reshape(-1, 2), axis=1)
                < SETTINGS.alignment_flow_error))
    src, dst = forward.reshape(-1, 2)[valid], points.reshape(-1, 2)[valid]
    if len(src) < SETTINGS.alignment_minimum_points:
        return None
    cv2.setRNGSeed(0)
    matrix, inliers = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC,
                                                ransacReprojThreshold=SETTINGS.alignment_reprojection_pixels)
    if matrix is None or inliers is None:
        return None
    good = inliers.ravel().astype(bool)
    if (good.sum() < SETTINGS.alignment_minimum_points
            or good.mean() < SETTINGS.alignment_inlier_fraction
            or abs(np.hypot(matrix[0, 0], matrix[1, 0])-1)
                > SETTINGS.alignment_maximum_scale_error
            or abs(np.degrees(np.arctan2(matrix[1, 0], matrix[0, 0])))
                > SETTINGS.alignment_maximum_rotation_degrees):
        return None
    if not refined:
        shift = np.median(dst[good]-src[good], axis=0)/scale
        if np.max(np.abs(shift)) > w*SETTINGS.alignment_legacy_shift_fraction:
            return None
        if np.max(np.abs(shift)) < SETTINGS.alignment_legacy_snap_pixels:
            shift[:] = 0
        return np.array([[1., 0., shift[0]], [0., 1., shift[1]]])
    if np.any(np.ptp(dst[good], axis=0)/np.array(size)
              < SETTINGS.alignment_minimum_spread_fraction):
        return None
    # Preserve the robust scale estimate, but do not introduce a rotation from
    # noisy optical flow. Refit translation after removing that rotation.
    zoom = float(np.hypot(matrix[0, 0], matrix[1, 0]))
    if abs(zoom-1) <= SETTINGS.alignment_scale_snap_ratio:
        zoom = 1.0
    shift = np.median(dst[good]-src[good]*zoom, axis=0)/scale
    transform = np.array([[zoom, 0, shift[0]], [0, zoom, shift[1]]], dtype=np.float64)
    corners = np.array([[x, y], [r, y], [x, b], [r, b]], dtype=np.float64)
    moved = corners*zoom+shift
    displacement = float(np.max(np.abs(moved-corners)))
    if displacement > w*SETTINGS.alignment_maximum_shift_fraction:
        return None
    if displacement < SETTINGS.alignment_snap_pixels:
        return np.array([[1., 0., 0.], [0., 1., 0.]])
    return transform


def _warp_patch(frame: NDArray[np.uint8], bounds: Bounds,
                transform: NDArray[np.float64]) -> tuple[NDArray[np.uint8], Bounds]:
    h, w = frame.shape[:2]
    x, y, r, b = bounds
    if np.array_equal(transform, np.array([[1., 0., 0.], [0., 1., 0.]])):
        return frame, bounds
    corners = np.array([[x, y, 1], [r, y, 1], [x, b, 1], [r, b, 1]])
    moved = corners @ transform.T
    aligned = cv2.warpAffine(frame, transform, (w, h), flags=cv2.INTER_LINEAR)
    shifted = (max(0, int(np.ceil(moved[:, 0].min()))),
               max(0, int(np.ceil(moved[:, 1].min()))),
               min(w, int(np.floor(moved[:, 0].max()))),
               min(h, int(np.floor(moved[:, 1].max()))))
    return np.asarray(aligned, dtype=np.uint8), shifted



def align_patch(frame: NDArray[np.uint8], canvas: NDArray[np.uint8],
                known: NDArray[np.bool_], bounds: Bounds) -> tuple[NDArray[np.uint8], Bounds]:
    transform = _patch_transform(frame, canvas, known, bounds)
    return (frame, bounds) if transform is None else _warp_patch(frame, bounds, transform)


SETTINGS = CanvasSettings()


def enabled(settings: ProjectSettings) -> bool:
    return (settings.photoshop_keep_model and settings.shorts_layout == "auto"
            and settings.preset in ("photoshop_shorts", "photoshop_music_shorts"))


def visible_bounds(camera: SubjectCamera, time_us: int, source_width: int,
                   source_height: int, width: int, height: int,
                   *, source_inset_fraction: float = 0) -> Bounds:
    """The source rectangle actually overlaid by camera_filter, excluding its border."""
    x, y, r, b = pixel_bounds(camera.viewport, source_width, source_height)
    cw, ch = size_at(camera, time_us)
    cx, cy = center_at(camera, time_us)
    scale = min(width / (cw * source_width), height / (ch * source_height))
    dynamic = any(p.width is not None or p.height is not None for p in camera.points)
    if dynamic:
        sw, sh = max(2, int((r-x)*scale/2)*2), max(2, int((b-y)*scale/2)*2)
    else:
        sw, sh = max(2, round((r-x)*scale/2)*2), max(2, round((b-y)*scale/2)*2)
    left = width/2 - (cx*source_width-x)*sw/(r-x)
    top = height/2 - (cy*source_height-y)*sh/(b-y)
    point = min(camera.points, key=lambda p: abs(p.time_us-time_us))
    sx, sy = sw/(r-x), sh/(b-y)
    if point.visible_region is not None:
        px, py, pr, pb = pixel_bounds(point.visible_region, source_width, source_height)
        left, top = left+(px-x)*sx, top+(py-y)*sy
        sw, sh = round((pr-px)*sx), round((pb-py)*sy)
    source_inset = (min(source_width, source_height)*source_inset_fraction
                    if point.visible_region is not None else 0)
    inset = max(SETTINGS.border_inset_pixels,
                round(min(sw, sh)*SETTINGS.source_border_fraction))
    ix = max(inset, int(np.ceil(source_inset*sx)))
    iy = max(inset, int(np.ceil(source_inset*sy)))
    return (max(0, min(width, int(np.ceil(left))+ix)),
            max(0, min(height, int(np.ceil(top))+iy)),
            max(0, min(width, int(np.floor(left+sw))-ix)),
            max(0, min(height, int(np.floor(top+sh))-iy)))


class CanvasMemory:
    """Keep only the current canvas and coverage, never a sequence of video frames."""

    def __init__(self, *, refined: bool = True) -> None:
        self.refined = refined
        self.canvas: NDArray[np.uint8] | None = None
        self.known: NDArray[np.bool_] | None = None
        self.references: list[tuple[NDArray[np.uint8], NDArray[np.bool_]]] = []
        self.active_reference: tuple[NDArray[np.uint8], NDArray[np.bool_]] | None = None
        self.updates = 0

    def reset(self) -> None:
        self.canvas = None
        self.known = None
        self.references.clear()
        self.active_reference = None
        self.updates = 0

    def update(self, frame: NDArray[np.uint8], bounds: Bounds,
               background_bgr: tuple[int, int, int] | None = None, *,
               refine_alignment: bool = True) -> NDArray[np.uint8]:
        bounds = trim_horizontal_chrome(frame, bounds)
        if self.canvas is None or self.canvas.shape != frame.shape:
            self.reset()
            self.canvas = np.empty_like(frame)
            self.canvas[:] = frame[0, 0] if background_bgr is None else background_bgr
            self.known = np.zeros(frame.shape[:2], dtype=np.bool_)
        assert self.known is not None
        if refine_alignment and np.any(self.known):
            # Register against immutable artwork snapshots first. Repeatedly
            # fitting to yesterday's resampled pixels accumulates scale drift.
            transform = None
            ordered = sorted(self.references, key=lambda item: item is not self.active_reference)
            for item in ordered:
                reference, coverage = item
                transform = _patch_transform(frame, reference, coverage, bounds)
                if transform is not None:
                    self.active_reference = item
                    break
            if transform is None:
                transform = _patch_transform(frame, self.canvas, self.known, bounds,
                                             refined=self.refined)
            if transform is not None:
                frame, bounds = _warp_patch(frame, bounds, transform)
        x, y, r, b = bounds
        if r <= x or b <= y:
            return self.canvas
        patch = frame[y:b, x:r]
        old = self.canvas[y:b, x:r]
        known = self.known[y:b, x:r]
        feather = SETTINGS.feather_pixels
        # Blend only at the viewport perimeter. The interior is replaced, not
        # max/lighten blended: new paint, erasures and darker edits all survive.
        yy, xx = np.ogrid[:b-y, :r-x]
        edge = np.minimum(np.minimum(yy, b-y-1-yy), np.minimum(xx, r-x-1-xx))
        alpha = np.minimum(1.0, (edge+1)/max(1, feather)).astype(np.float32)
        alpha[~known] = 1
        weight = alpha[:, :, None]
        old[:] = np.rint(patch*weight + old*(1-weight)).astype(np.uint8)
        known[:] = True
        if not self.refined:
            from sculptcut.canvas_edges import clear_outer_guides

            for lo, hi, row in ((0, y, y), (b, frame.shape[0], b-1)):
                if hi <= lo:
                    continue
                colour = np.median(frame[row, x:r], axis=0)
                margin = self.canvas[lo:hi]
                distance = np.linalg.norm(margin.astype(np.float32)-colour, axis=2)
                # Refresh the entire margin only when every pixel is background.
                # A partial colour mask would leave patches on a gradient.
                if (np.ptp(colour) >= SETTINGS.margin_background_chroma
                        and np.max(distance) < SETTINGS.margin_background_distance
                        and np.max(np.linalg.norm(self.canvas[row].astype(np.float32)-colour,
                                                  axis=1)) < SETTINGS.margin_background_distance):
                    margin[:] = self.canvas[row]
            clear_outer_guides(self.canvas)
        self.updates += 1
        if self.refined and refine_alignment and (not self.references
                or self.updates % SETTINGS.alignment_reference_interval == 0):
            # Fixed-size ring: the earliest usable view plus recent development.
            # Never store the source sequence or use future paint as output.
            if _patch_transform(self.canvas, self.canvas, self.known, bounds) is not None:
                self.references.append((self.canvas.copy(), self.known.copy()))
                if len(self.references) > SETTINGS.alignment_reference_limit:
                    del self.references[1]
        # Unseen top/bottom margins continue the visible canvas background.
        # A fixed reference colour otherwise leaves a separate coloured strip
        # beside a gradient canvas, even after its black window border is gone.
        rows = np.flatnonzero(np.any(self.known, axis=1))
        first, last = int(rows[0]), int(rows[-1])
        for lo, hi, edge_row in ((0, first, first), (last+1, frame.shape[0], last)):
            if hi > lo:
                colour = np.median(self.canvas[edge_row][self.known[edge_row]], axis=0)
                self.canvas[lo:hi][~self.known[lo:hi]] = colour
        return self.canvas


def update_closeup(memory: CanvasMemory, frame: NDArray[np.uint8], camera: SubjectCamera,
                   time_us: int, source_width: int, source_height: int
                   ) -> NDArray[np.uint8] | None:
    """Paste only aligned, verified document pixels into the local artwork."""
    point = min(camera.points, key=lambda p: abs(p.time_us-time_us))
    if not point.reference_matched or point.visible_region is None:
        return memory.canvas
    height, width = frame.shape[:2]
    bounds = visible_bounds(camera, time_us, source_width, source_height, width, height,
                            source_inset_fraction=SETTINGS.registered_source_inset_fraction)
    x, y, r, b = bounds
    if r <= x or b <= y:
        return memory.canvas
    if point.flip_horizontal:
        frame = np.asarray(cv2.flip(frame, 1), dtype=np.uint8)
        bounds = width-r, y, width-x, b
    rgb = camera.background_rgb or (camera.background,)*3
    return memory.update(frame, bounds, (rgb[2], rgb[1], rgb[0]), refine_alignment=False)


def compose_history(chunks: Sequence[tuple[Path, "RenderDecision"]],
                    sources: dict[str, SourceMedia], destination: Path, fps: int,
                    width: int, height: int, cancel: Event | None = None,
                    progress: Callable[[float], None] | None = None,
                    *, page_alignment: bool = False, accumulate: bool = True,
                    guard_document_edges: bool = False) -> None:
    """Lossless intermediate; the normal delivery pass supplies SDR tags and audio."""
    if cancel and cancel.is_set():
        raise Cancelled("Canvas assembly cancelled")
    memory = CanvasMemory(refined=page_alignment)
    local = CanvasMemory()
    local_group = None
    last_closeup = None
    total = sum(d.output_frame_count for _, d in chunks)
    completed = 0
    writer = cv2.VideoWriter(str(destination), cv2.VideoWriter.fourcc(*"FFV1"),
                             fps, (width, height))
    if not writer.isOpened():
        raise MediaError("Cannot open lossless canvas encoder")
    success = False
    try:
        for path, decision in chunks:
            camera = decision.subject_camera
            closeup = (camera is not None and camera.frame_only and camera.fit_subject
                       and decision.action in ("SPEED", "KEEP") and decision.speed_ramp is None)
            group = camera.closeup_group if camera is not None and accumulate and closeup else None
            if group != local_group:
                local.reset()
                local_group = group
            if group is None:
                last_closeup = None
            reliable = (camera is not None and camera.canvas_registered and camera.fit_subject
                        and decision.speed_ramp is None)
            source = sources[decision.source_id]
            sw, sh = source.width, source.height
            if source.rotation % 180 == 90:
                sw, sh = sh, sw
            reader = cv2.VideoCapture(str(path))
            times = [p.time_us for p in camera.points] if camera else []
            try:
                for index in range(decision.output_frame_count):
                    if cancel and cancel.is_set():
                        raise Cancelled("Canvas assembly cancelled")
                    ok, decoded = reader.read()
                    if not ok or decoded.shape[:2] != (height, width):
                        raise MediaError("Cannot decode canvas chunk")
                    frame = np.asarray(decoded, dtype=np.uint8)
                    displayed_current = False
                    if group is not None and camera is not None:
                        t = decision.source_start_us + (
                            (decision.source_end_us-decision.source_start_us)*index
                            // decision.output_frame_count)
                        current = update_closeup(local, frame, camera, t, sw, sh)
                        if current is not None:
                            last_closeup = current
                        if last_closeup is not None:
                            frame = last_closeup
                            displayed_current = True
                        elif memory.canvas is None:
                            frame = np.full_like(frame, camera.background)
                    elif camera is not None and (not accumulate or closeup):
                        t = decision.source_start_us + (
                            (decision.source_end_us-decision.source_start_us)*index
                            // decision.output_frame_count)
                        point = min(camera.points, key=lambda p: abs(p.time_us-t))
                        if not closeup or point.visible_region is not None:
                            bounds = visible_bounds(camera, t, sw, sh, width, height)
                            x,y,r,b = trim_horizontal_chrome(frame, bounds)
                            rgb = camera.background_rgb or (camera.background,)*3
                            current = np.empty_like(frame)
                            current[:] = (rgb[2], rgb[1], rgb[0])
                            current[y:b, x:r] = frame[y:b, x:r]
                            frame = current
                            displayed_current = True
                        elif memory.canvas is None:
                            # Missing local evidence cannot seed the shared canvas.
                            frame = np.full_like(frame, camera.background)
                    elif reliable and camera is not None:
                        # camera_filter evaluates points on the rendered output clock.
                        still = decision.action == "HOLD" or decision.output_frame_count == 1
                        t = decision.source_start_us + (round(index*1e6/fps) if still else
                            (decision.source_end_us-decision.source_start_us)*index
                            // decision.output_frame_count)
                        point_index = min(range(max(0, bisect_left(times, t)-1),
                                                min(len(times), bisect_left(times, t)+1)),
                                          key=lambda i: abs(times[i]-t))
                        if camera.points[point_index].reference_matched:
                            displayed_current = True
                            rgb = camera.background_rgb or (camera.background,)*3
                            # The tracking rectangle can include antialiased
                            # pixels just outside the drawn page. At high zoom
                            # even one source row can stain the saved margin.
                            inset = (SETTINGS.registered_source_inset_fraction
                                     if guard_document_edges else 0)
                            frame = memory.update(frame, visible_bounds(
                                camera, t, sw, sh, width, height,
                                source_inset_fraction=inset), (rgb[2], rgb[1], rgb[0]))
                    if not displayed_current:
                        if memory.canvas is not None:
                            # Lost correspondence is not evidence of a new drawing.
                            # Preserve trusted pixels until matching resumes; never
                            # paste the fallback view or clear the canvas on a timer.
                            frame = memory.canvas
                    writer.write(frame)
                    completed += 1
                    if progress:
                        progress(completed/max(1, total))
            finally:
                reader.release()
        success = True
    except cv2.error as exc:
        raise MediaError(f"Canvas assembly failed: {exc}") from exc
    finally:
        writer.release()
        if not success:
            destination.unlink(missing_ok=True)
