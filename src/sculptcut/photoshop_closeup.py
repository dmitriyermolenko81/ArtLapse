"""Local Photoshop shots for long gaps in the shared canvas registration."""

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from itertools import islice
from math import ceil
from pathlib import Path
from threading import Event

import cv2
import numpy as np
from numpy.typing import NDArray

from sculptcut.canvas_edges import Bounds
from sculptcut.domain import CameraPoint, Project, Roi, SubjectCamera
from sculptcut.drawing_reference import DrawingReference, locked_reference_points
from sculptcut.media import Cancelled, MediaError
from sculptcut.planning import RenderDecision
from sculptcut.roi import pixel_bounds
from sculptcut.webcam import AnalysisCapture, webcam_roi


@dataclass(frozen=True)
class CloseupSettings:
    minimum_gap_seconds: float = 3.0
    aspect: float = 9/16
    margin: float = 1.08
    inset_fraction: float = .012
    smoothing_radius: int = 2
    maximum_subject_coverage: float = .85
    lower_edge_fraction: float = .02
    minimum_upper_clearance: float = .10
    upper_focus_fraction: float = .52
    mask_red_minimum: int = 76
    mask_green_maximum: int = 62
    mask_blue_maximum: int = 65
    mask_dominance: int = 16
    mask_minimum_coverage: float = .35
    mask_edge_coverage: float = .70
    mask_maximum_seconds: float = 2.5
    orientation_score_ratio: float = 1.5
    orientation_anchors_per_side: int = 2
    reference_lookahead: int = 4
    reference_subject_center_range: float = .42
    reference_minimum_coverage: float = .65
    patch_minimum_coverage: float = .35


SETTINGS = CloseupSettings()


def red_mask_candidate(frame: NDArray[np.uint8], bounds: Bounds) -> bool:
    """Look for a strong red cast including the edges of a verified document."""
    x, y, r, b = bounds
    pixels = frame[y:b:3, x:r:3].astype(np.int16)
    blue, green, red = np.moveaxis(pixels, -1, 0)
    mask = ((red >= SETTINGS.mask_red_minimum) & (green <= SETTINGS.mask_green_maximum)
            & (blue <= SETTINGS.mask_blue_maximum)
            & (red >= 2*green+SETTINGS.mask_dominance)
            & (red >= 2*blue+SETTINGS.mask_dominance))
    if np.mean(mask) < SETTINGS.mask_minimum_coverage:
        return False
    h, w = mask.shape
    centre = mask[h//8:7*h//8]
    edge = max(1, w//8)
    return min(float(np.mean(centre[:, :edge])), float(np.mean(centre[:, -edge:]))) >= (
        SETTINGS.mask_edge_coverage)


def transient_masks(samples: list[tuple[int, int, bool | None]], fps: int
                    ) -> Iterator[tuple[int, int]]:
    """A short red interval surrounded by verified clear views may be Quick Mask."""
    start = 0
    for end, (_, _, state) in enumerate(samples):
        if state is True:
            continue
        if (state is False and end > start > 0
                and samples[start-1][2] is False
                and end-start <= fps*SETTINGS.mask_maximum_seconds):
            yield from ((shot, frame) for shot, frame, _ in samples[start:end])
        start = end+1


def long_gaps(decisions: Sequence[RenderDecision], fps: int) -> Iterator[list[int]]:
    """Skip invisible decisions on the output clock and preserve deliberate stops."""
    frames = 0
    gap: list[int] = []
    minimum = ceil(fps*SETTINGS.minimum_gap_seconds)
    for index, decision in enumerate(decisions):
        if not decision.output_frame_count:
            continue
        camera = decision.subject_camera
        if (decision.action in ("KEEP", "SPEED") and decision.speed_ramp is None
                and (camera is None or not camera.canvas_registered)):
            frames += decision.output_frame_count
            gap.append(index)
        else:
            if frames >= minimum:
                yield gap
            frames = 0
            gap = []
    if frames >= minimum:
        yield gap


def horizontal_focus(frame: NDArray[np.uint8], box: Roi | None,
                     document: Roi | None) -> float | None:
    """Keep the upper artwork in frame when a zoomed-in lower part is clipped."""
    if box is None or document is None:
        return None
    h, w = frame.shape[:2]
    portrait_width = document.height*h/w*SETTINGS.aspect*(1-2*SETTINGS.inset_fraction)
    if (box.width <= portrait_width
            or box.y+box.height < document.y+document.height*(1-SETTINGS.lower_edge_fraction)
            or box.y < document.y+document.height*SETTINGS.minimum_upper_clearance):
        return None
    from sculptcut.photoshop import drawing_and_canvas_box

    x, y, right, bottom = pixel_bounds(document, w, h)
    end = min(bottom, round((box.y+box.height*SETTINGS.upper_focus_fraction)*h))
    focus, _ = drawing_and_canvas_box(frame[y:end, x:right])
    if focus is None:
        return None
    return (x+(focus.x+focus.width/2)*(right-x))/w


def closeup_camera(times: Sequence[int], subjects: Sequence[Roi | None],
                   documents: Sequence[Roi | None], width: int, height: int,
                   focuses: Sequence[float | None] | None = None,
                   ) -> SubjectCamera | None:
    """Fit the visible work only when every frame independently confirms a document."""
    if any(view is None for view in documents):
        return None
    views = [view for view in documents if view is not None]
    if not views:
        return None
    measurements = {i: (focus if focus is not None else box.x+box.width/2,
                        box.y+box.height/2, box.width, box.height)
                    for i, (box, view, focus) in enumerate(zip(
                        subjects, views, focuses or [None]*len(views), strict=True))
                    if box is not None
                    and view.x <= box.x+box.width/2 <= view.x+view.width
                    and view.y <= box.y+box.height/2 <= view.y+view.height
                    and box.width*box.height <= (
                        view.width*view.height*SETTINGS.maximum_subject_coverage)}
    if len(measurements)*2 < len(times):
        return None

    # Local median suppresses cursor jitter while respecting a camera cut at each shot.
    aspect = SETTINGS.aspect*height/width
    points = []
    for i, (time, view) in enumerate(zip(times, views, strict=True)):
        around = [pose for j in range(i-SETTINGS.smoothing_radius, i+SETTINGS.smoothing_radius+1)
                  if (pose := measurements.get(j)) is not None]
        cx, cy, w, h = np.median(around or [measurements[min(
            measurements, key=lambda j: abs(j-i))]], axis=0)
        inset = SETTINGS.inset_fraction
        ch = min(max(h, w/aspect)*SETTINGS.margin,
                 view.height*(1-2*inset), view.width*(1-2*inset)/aspect)
        cw = ch*aspect
        x = float(np.clip(cx, view.x+view.width*inset+cw/2,
                         view.x+view.width*(1-inset)-cw/2))
        y = float(np.clip(cy, view.y+view.height*inset+ch/2,
                         view.y+view.height*(1-inset)-ch/2))
        points.append(CameraPoint(time_us=time, x=x, y=y, width=float(cw), height=float(ch),
                                  visible_region=view))
    left, top = min(v.x for v in views), min(v.y for v in views)
    right = min(1., max(v.x+v.width for v in views))
    bottom = min(1., max(v.y+v.height for v in views))
    return SubjectCamera(
        viewport=Roi(x=left, y=top, width=right-left, height=bottom-top),
        frame_only=True, fit_subject=True, width=points[0].width or 1, height=points[0].height or 1,
        points=points, background=32,
    )


def reflected_view(view: Roi) -> Roi:
    return view.model_copy(update={"x": max(0., 1-view.x-view.width)})


@dataclass(frozen=True)
class CloseupSample:
    frame: NDArray[np.uint8]
    view: Roi
    flipped: NDArray[np.uint8]
    features: tuple[tuple[cv2.KeyPoint, ...], NDArray[np.float32] | None]
    flipped_features: tuple[tuple[cv2.KeyPoint, ...], NDArray[np.float32] | None]

    @classmethod
    def from_frame(cls, frame: NDArray[np.uint8], view: Roi,
                   extractor: DrawingReference) -> "CloseupSample":
        flipped = np.asarray(cv2.flip(frame, 1), dtype=np.uint8)
        return cls(frame, view, flipped, extractor.features(frame, view),
                   extractor.features(flipped, reflected_view(view)))

    def match(self, reference: DrawingReference, time_us: int) -> tuple[CameraPoint | None, float]:
        point = reference.point(self.frame, self.view, time_us, self.features)
        direct = reference.last_score
        mirror = reference.point(self.flipped, reflected_view(self.view), time_us,
                                 self.flipped_features)
        inverse = reference.last_score
        if point is not None and direct > inverse*SETTINGS.orientation_score_ratio:
            return point.model_copy(update={"visible_region": self.view}), direct
        if mirror is not None and inverse > direct*SETTINGS.orientation_score_ratio:
            # The export camera reads the original coordinates. The already
            # cropped patch is flipped before it is written to local history.
            return mirror.model_copy(update={"x": 1-mirror.x, "flip_horizontal": True,
                                             "visible_region": self.view}), inverse
        return None, 0.


def coverage(point: CameraPoint, view: Roi) -> float:
    assert point.width is not None and point.height is not None
    w, h = point.width, point.height
    x = min(point.x+w/2, view.x+view.width)-max(point.x-w/2, view.x)
    y = min(point.y+h/2, view.y+view.height)-max(point.y-h/2, view.y)
    return max(0., x)*max(0., y)/(w*h)


def same_framing(point: CameraPoint | None, target: CameraPoint, view: Roi) -> bool:
    if point is None:
        return False
    assert point.width is not None and point.height is not None
    spread = SETTINGS.reference_subject_center_range
    return (coverage(point, view) >= SETTINGS.reference_minimum_coverage
            and abs(point.x-target.x) <= point.width*spread
            and abs(point.y-target.y) <= point.height*spread)


@dataclass(frozen=True)
class CloseupProbe:
    sample: CloseupSample
    framing: CameraPoint
    reference: DrawingReference | None

    def compare(self, reference: DrawingReference) -> float:
        point, score = self.sample.match(reference, 0)
        return score if same_framing(point, self.framing, self.sample.view) else 0.


def best_closeup_reference(probes: Sequence[CloseupProbe | None]) -> DrawingReference | None:
    best, best_rank = None, (0, 0.)
    for candidate in probes:
        if candidate is None or candidate.reference is None:
            continue
        matches, strength = 0, 0.
        for probe in probes:
            score = probe.compare(candidate.reference) if probe is not None else 0.
            if not score:
                break
            matches += 1
            # Self matches do not tell us how well another shot lines up.
            if probe is not candidate:
                strength += score
        if (matches, strength) > best_rank:
            best, best_rank = candidate.reference, (matches, strength)
    return best


def add_closeups(project: Project, decisions: list[RenderDecision], fps: int,
                 clips: dict[int, tuple[Path, list[int], Roi]],
                 subjects: dict[int, list[Roi | None]], documents: dict[int, list[Roi | None]],
                 focuses: dict[int, list[float | None]], cancel: Event | None = None,
                 progress: Callable[[float], None] | None = None) -> int:
    runs = list(long_gaps(decisions, fps))
    total = sum(map(len, runs))
    completed = added = 0

    def check_cancel() -> None:
        if cancel and cancel.is_set():
            raise Cancelled("Photoshop close-up fitting cancelled")

    def open_clip(index: int) -> AnalysisCapture:
        check_cancel()
        return AnalysisCapture(clips[index][0], webcam_roi(
            project.settings, decisions[index].source_id))

    def still(index: int, at: int) -> NDArray[np.uint8]:
        reader = open_clip(index)
        try:
            for _ in range(at):
                check_cancel()
                if not reader.grab():
                    raise MediaError("Cannot seek Photoshop close-up clip")
            ok, frame = reader.read()
            if not ok:
                raise MediaError("Cannot decode Photoshop close-up clip")
            return np.asarray(frame, dtype=np.uint8)
        finally:
            reader.release()

    def anchor(indices: range, reverse: bool) -> Iterator[DrawingReference]:
        for index in indices:
            camera = decisions[index].subject_camera
            if camera is None or not camera.canvas_registered or index not in clips:
                continue
            times = clips[index][1]
            points = reversed(camera.points) if reverse else iter(camera.points)
            for point in points:
                view = point.visible_region
                if point.reference_matched and view is not None and point.time_us in times:
                    yield DrawingReference(still(index, times.index(point.time_us)), view,
                                           camera.model_copy(update={"points": [point]}))
                    break

    def probe(index: int, anchors: list[DrawingReference]) -> CloseupProbe | None:
        _, times, _ = clips[index]
        candidates = [k for k, view in enumerate(documents[index]) if view is not None
                      and subjects[index][k] is not None]
        if not candidates:
            return None
        j = min(candidates, key=lambda k: abs(k-len(times)//2))
        image = still(index, j)
        view = documents[index][j]
        assert view is not None
        h, w = image.shape[:2]
        seed = closeup_camera([times[j]], [subjects[index][j]], [view], w, h,
                              [focuses[index][j]])
        if seed is None:
            return None
        sample = CloseupSample.from_frame(image, view, anchors[0])
        direct = inverse = 0.
        for anchor_reference in anchors:
            point, score = sample.match(anchor_reference, times[j])
            if point is not None and point.flip_horizontal:
                inverse = max(inverse, score)
            elif point is not None:
                direct = max(direct, score)
        reference: DrawingReference | None = None
        if direct > inverse*SETTINGS.orientation_score_ratio:
            reference = DrawingReference(image, view, seed)
        elif inverse > direct*SETTINGS.orientation_score_ratio:
            mirrored = reflected_view(view)
            camera = seed.model_copy(update={
                "viewport": mirrored,
                "points": [seed.points[0].model_copy(update={"x": 1-seed.points[0].x})],
            })
            reference = DrawingReference(sample.flipped, mirrored, camera)
        return CloseupProbe(sample, seed.points[0], reference)

    for run in runs:
        count = SETTINGS.orientation_anchors_per_side
        anchors = [*islice(anchor(range(run[0]-1, -1, -1), True), count),
                   *islice(anchor(range(run[-1]+1, len(decisions)), False), count)]
        # Without evidence of the montage's orientation, retain the trusted
        # shared canvas. A clean viewport alone cannot identify a mirror.
        if not anchors:
            completed += len(run)
            continue
        probes: dict[int, CloseupProbe | None] = {}
        reference = None
        group = run[0]
        for offset, index in enumerate(run):
            upcoming = run[offset:offset+SETTINGS.reference_lookahead]
            for j in upcoming:
                if j not in probes:
                    probes[j] = probe(j, anchors)
            shot = probes[index]
            if shot is not None and (reference is None or not shot.compare(reference)):
                best = best_closeup_reference([probes[j] for j in upcoming])
                if best is not None:
                    reference, group = best, index

            if reference is not None:
                decision = decisions[index]
                _, times, _ = clips[index]
                points = []
                reader = open_clip(index)
                try:
                    for t, view in zip(times, documents[index], strict=True):
                        check_cancel()
                        ok, frame = reader.read()
                        if not ok:
                            raise MediaError("Cannot decode Photoshop close-up clip")
                        point = None
                        if view is not None:
                            sample = CloseupSample.from_frame(frame, view, reference)
                            point, _ = sample.match(reference, t)
                            if point is not None and coverage(point, view) < (
                                    SETTINGS.patch_minimum_coverage):
                                point = None
                        points.append(point)
                finally:
                    reader.release()
                matched = [p for p in points if p is not None]
                views = [p.visible_region for p in matched if p.visible_region is not None]
                if not views:
                    # Keep the local group alive through an unverified shot.
                    views = [reference.camera.viewport]
                left, top = min(v.x for v in views), min(v.y for v in views)
                right = min(1., max(v.x+v.width for v in views))
                bottom = min(1., max(v.y+v.height for v in views))
                poses = locked_reference_points(points, times) or [
                    reference.camera.points[0].model_copy(update={"time_us": times[0]})]
                if matched:
                    poses = [p.model_copy(update={
                        "reference_matched": raw is not None,
                        "visible_region": raw.visible_region if raw else None,
                        "flip_horizontal": bool(raw and raw.flip_horizontal),
                    }) for p, raw in zip(poses, points, strict=True)]
                camera = reference.camera.model_copy(update={
                    "viewport": Roi(x=left, y=top, width=right-left, height=bottom-top),
                    "points": poses, "closeup_group": group,
                })
                decisions[index] = decision.model_copy(update={"subject_camera": camera})
                added += bool(matched)
            del probes[index]
            completed += 1
            if progress:
                progress(completed/total)
    if progress:
        progress(1.)
    return added
