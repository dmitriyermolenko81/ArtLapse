"""Stable horizontal-only square cameras from bounded episode observations."""

from pathlib import Path
from threading import Event

import cv2
import numpy as np

from sculptcut.analysis import Gray
from sculptcut.domain import CameraPoint, ProjectSettings, Roi, SourceMedia, SubjectCamera
from sculptcut.media import Cancelled, MediaError
from sculptcut.shorts import sculpt_footprint, viewport_box
from sculptcut.subject_camera import tracking_clip
from sculptcut.webcam import AnalysisCapture, webcam_roi


def horizontal_camera(
    roi: Roi,
    samples: list[tuple[int, Roi]],
    settings: ProjectSettings,
    start_us: int,
    previous_x: float | None = None,
    viewport: Roi | None = None,
) -> SubjectCamera:
    """Lock a whole episode when possible; pan only when a fixed square cannot fit it."""
    half = roi.width / 2
    viewport = viewport or Roi(x=0, y=roi.y, width=1, height=roi.height)
    lower = viewport.x + half
    upper = viewport.x + viewport.width - half
    if lower > upper:
        lower = upper = viewport.x + viewport.width / 2
    margin = roi.width * settings.square_tracking_margin
    initial = roi.x + half if previous_x is None else previous_x
    initial = max(lower, min(upper, initial))
    cy = roi.y + roi.height / 2
    points = [CameraPoint(time_us=start_us, x=initial, y=cy)]
    if samples:
        # A three-sample median rejects one-frame menu detections and boundary jitter.
        filtered = []
        for index, (time_us, _) in enumerate(samples):
            window = (
                samples[index : index + 1]
                if index in (0, len(samples) - 1)
                else samples[index - 1 : index + 2]
            )
            left = float(np.median([box.x for _, box in window]))
            right = float(np.median([box.x + box.width for _, box in window]))
            filtered.append((time_us, left, right))
        left = min(x for _, x, _ in filtered)
        right = max(x for _, _, x in filtered)
        low, high = max(lower, right + margin - half), min(upper, left - margin + half)
        if low <= high:
            # One fixed centered camera for the complete episode is more stable than following.
            target = max(low, min(high, (left + right) / 2))
            if abs(target - initial) <= roi.width * settings.square_tracking_dead_zone:
                target = max(low, min(high, initial))
            points = [CameraPoint(time_us=start_us, x=target, y=cy)]
        else:
            center = initial
            points = []
            for index, (time_us, _, _) in enumerate(filtered):
                # Look ahead one sample to begin a gradual pan before the object reaches an edge.
                lookahead = filtered[max(0, index - 1) : min(len(filtered), index + 2)]
                left = min(x for _, x, _ in lookahead)
                right = max(x for _, _, x in lookahead)
                low = max(lower, right + margin - half)
                high = min(upper, left - margin + half)
                if low <= high:
                    center = max(low, min(high, center))
                else:
                    center = max(lower, min(upper, (left + right) / 2))
                points.append(CameraPoint(time_us=time_us, x=center, y=cy))
    return SubjectCamera(
        viewport=viewport,
        fit_subject=True,
        width=roi.width,
        height=roi.height,
        points=points,
        background=24,
    )


def measure_square_episode(
    source: SourceMedia,
    roi: Roi,
    start_us: int,
    end_us: int,
    output_frames: int,
    output_fps: int,
    hold: bool,
    settings: ProjectSettings,
    scratch: Path,
    cancel: Event | None,
    viewports: list[Roi] | None = None,
    manual_viewport: Roi | None = None,
) -> list[tuple[int, Roi]]:
    rate = settings.square_tracking_fps
    frames = 1 if hold else max(1, round(output_frames / output_fps * rate))
    end = (
        min(
            source.duration_us,
            start_us + max(1, round(1_000_000 * source.fps_den / max(1, source.fps_num))),
        )
        if hold
        else end_us
    )
    clip = tracking_clip(
        source, start_us, end, scratch, cancel, output_frames=frames, output_fps=rate
    )
    capture = AnalysisCapture(clip, webcam_roi(settings, source.id))
    samples: list[tuple[int, Roi]] = []
    previous = roi

    def canvas(gray: Gray) -> Roi:
        detected = manual_viewport or viewport_box(gray)
        if detected is None:
            return roi  # An uncertain canvas must never enable panning into the full UI.
        if manual_viewport is None:
            h, w = gray.shape
            left = detected.x + (1 / w if detected.x > 0 else 0)
            top = detected.y + (1 / h if detected.y > 0 else 0)
            right = detected.x + detected.width - (1 / w if detected.x + detected.width < 1 else 0)
            bottom = (
                detected.y + detected.height - (1 / h if detected.y + detected.height < 1 else 0)
            )
            if right > left and bottom > top:
                detected = Roi(x=left, y=top, width=right - left, height=bottom - top)
        if viewports is not None:
            viewports.append(detected)
        return detected

    try:
        if not capture.isOpened():
            raise MediaError("Cannot read square camera observations")
        index = 0
        while True:
            if cancel and cancel.is_set():
                raise Cancelled("Square camera analysis cancelled")
            ok, frame = capture.read()
            if not ok:
                break
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.uint8)
            viewport = canvas(gray)
            # Search the entire horizontal canvas, including parts outside the selected square.
            top, bottom = (
                max(roi.y, viewport.y),
                min(roi.y + roi.height, viewport.y + viewport.height),
            )
            if bottom <= top:
                viewport = roi
                top, bottom = roi.y, roi.y + roi.height
            band = Roi(x=viewport.x, y=top, width=viewport.width, height=bottom - top)
            box = sculpt_footprint(gray, band, previous)
            if box is not None:
                time_us = start_us + min(
                    end_us - start_us - 1, index * (end_us - start_us) // frames
                )
                samples.append((time_us, box))
                previous = box
            index += 1
    finally:
        capture.release()
    if not hold and frames > 1:
        frame_us = max(1, round(1_000_000 * source.fps_den / max(1, source.fps_num)))
        for first in (True, False):
            edge_start = start_us if first else max(start_us, end_us - frame_us)
            edge_end = min(end_us, start_us + frame_us) if first else end_us
            tracking_clip(
                source, edge_start, edge_end, scratch, cancel, output_frames=1, output_fps=rate
            )
            edge = AnalysisCapture(scratch, webcam_roi(settings, source.id))
            try:
                ok, frame = edge.read()
                if ok:
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.uint8)
                    viewport = canvas(gray)
                    band = Roi(x=viewport.x, y=roi.y, width=viewport.width, height=roi.height)
                    box = sculpt_footprint(gray, band, previous)
                    if box is not None:
                        timestamp = start_us if first else end_us - 1
                        samples = [(t, b) for t, b in samples if t != timestamp]
                        samples.append((timestamp, box))
            finally:
                edge.release()
    return sorted(samples, key=lambda sample: sample[0])


def safe_viewport(viewports: list[Roi], roi: Roi) -> Roi:
    """One conservative canvas for the episode, shared by camera and pixel clipping."""
    if not viewports:
        return roi
    left = max(v.x for v in viewports)
    right = min(v.x + v.width for v in viewports)
    top = max(roi.y, *(v.y for v in viewports))
    bottom = min(roi.y + roi.height, *(v.y + v.height for v in viewports))
    if right <= left or bottom <= top:
        return roi
    return Roi(x=left, y=top, width=right - left, height=bottom - top)
