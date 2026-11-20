"""Exclude thin horizontal window chrome near the visible canvas perimeter."""

from dataclasses import dataclass

import cv2
import numpy as np
from numpy.typing import NDArray

Bounds = tuple[int, int, int, int]


@dataclass(frozen=True)
class EdgeSettings:
    search_fraction: float = .15
    minimum_row_coverage: float = .9
    maximum_dark_level: float = 85
    minimum_contrast: float = 50
    maximum_band_fraction: float = .06
    padding_fraction: float = .015
    scrollbar_proximity_fraction: float = .05
    scrollbar_row_coverage: float = .6
    scrollbar_maximum_dark_level: float = 130
    scrollbar_minimum_contrast: float = 30
    guide_minimum_contrast: float = 2
    guide_maximum_contrast: float = 35
    guide_width_fraction: float = .35
    guide_height_fraction: float = .04
    foreground_row_fraction: float = .03


SETTINGS = EdgeSettings()


@dataclass(frozen=True)
class WorkspaceSettings:
    neutral_chroma: int = 10
    chrome_level: int = 56
    chrome_coverage: float = .70
    top_range: tuple[float, float] = (.025, .12)
    bottom_range: tuple[float, float] = (.008, .13)
    left_range: tuple[float, float] = (.008, .085)
    right_range: tuple[float, float] = (.002, .085)
    chromatic_level: int = 14
    document_chromatic_coverage: float = .12
    document_line_coverage: float = .22
    minimum_document_area: float = .08
    document_gap_pixels: int = 2


WORKSPACE = WorkspaceSettings()


def _chrome_extent(profile: NDArray[np.float64],
                   limits: tuple[float, float]) -> int | None:
    minimum, maximum = (round(len(profile)*fraction) for fraction in limits)
    hits = np.flatnonzero(profile[:maximum] >= WORKSPACE.chrome_coverage)
    if not len(hits) or not minimum <= hits[-1]+1 < maximum-1:
        return None
    return int(hits[-1]+1)


def _document_extent(profile: NDArray[np.float64]) -> tuple[int, int] | None:
    lines = np.flatnonzero(profile >= WORKSPACE.document_line_coverage)
    if not len(lines):
        return None
    # Transform guides and brush rings can light up a line well outside the
    # sheet. Use the connected document extent, bridging only codec gaps.
    breaks = np.flatnonzero(np.diff(lines) > WORKSPACE.document_gap_pixels+1)+1
    document = max(np.split(lines, breaks), key=len)
    return int(document[0]), int(document[-1])+1


def dark_document_bounds(frame: NDArray[np.uint8]) -> Bounds | None:
    """Find a coloured document between four neutral bars in the dark workspace."""
    h, w = frame.shape[:2]
    blue, green, red = cv2.split(frame)
    brightest = np.maximum(np.maximum(blue, green), red)
    darkest = np.minimum(np.minimum(blue, green), red)
    chroma = brightest-darkest
    neutral = chroma <= WORKSPACE.neutral_chroma
    chrome = neutral & (darkest >= WORKSPACE.chrome_level)
    rows = np.mean(chrome[:, w//8:7*w//8], axis=1)
    columns = np.mean(chrome[h//5:4*h//5, :], axis=0)
    top = _chrome_extent(rows, WORKSPACE.top_range)
    bottom = _chrome_extent(rows[::-1], WORKSPACE.bottom_range)
    left = _chrome_extent(columns, WORKSPACE.left_range)
    right = _chrome_extent(columns[::-1], WORKSPACE.right_range)
    if top is None or bottom is None or left is None or right is None:
        return None
    x, y, r, b = left+1, top+1, w-right-1, h-bottom-1
    workspace = neutral & (brightest <= WORKSPACE.chrome_level)
    content = ~workspace[y:b, x:r]
    cols = _document_extent(np.mean(content, axis=0))
    if cols is None:
        return None
    lo, hi = cols
    lines = _document_extent(np.mean(content[:, lo:hi], axis=1))
    if lines is None:
        return None
    top, bottom = lines
    if (hi-lo)*(bottom-top) < w*h*WORKSPACE.minimum_document_area:
        return None
    x, y, r, b = x+lo, y+top, x+hi, y+bottom
    if np.mean(chroma[y:b, x:r] >= WORKSPACE.chromatic_level) < (
        WORKSPACE.document_chromatic_coverage
    ):
        return None
    return x, y, r, b


def clear_outer_guides(frame: NDArray[np.uint8]) -> None:
    """Inpaint thin guide rectangles above the figure, never its interior."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.uint8)
    h, w = gray.shape
    background = np.median(gray, axis=1)
    delta = background[:, None]-gray.astype(np.float32)
    subject = np.flatnonzero(np.mean(delta > SETTINGS.minimum_contrast, axis=1)
                            >= SETTINGS.foreground_row_fraction)
    if not len(subject):
        return
    limit = min(int(subject[0]), round(h*SETTINGS.search_fraction))
    if limit < 3:
        return
    smooth = cv2.GaussianBlur(frame, (0, 0), 2).astype(np.float32)
    guide_delta = np.linalg.norm(smooth[:limit]-frame[:limit].astype(np.float32), axis=2)
    mask = ((guide_delta >= SETTINGS.guide_minimum_contrast)
            & (guide_delta <= SETTINGS.guide_maximum_contrast)).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((1, 3), np.uint8))
    horizontal = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
        np.ones((1, max(3, round(w*SETTINGS.guide_width_fraction))), np.uint8))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(horizontal)
    erase = np.zeros_like(gray)
    for i in range(1, count):
        if (stats[i, cv2.CC_STAT_HEIGHT] <= h*SETTINGS.guide_height_fraction
                and np.any(horizontal[labels == i])):
            erase[:limit][labels == i] = 255
    if np.any(erase):
        erase = cv2.dilate(erase, np.ones((3, 3), np.uint8)).astype(np.uint8)
        frame[:] = cv2.inpaint(frame, erase, 3, cv2.INPAINT_TELEA)


def trim_horizontal_chrome(frame: NDArray[np.uint8], bounds: Bounds) -> Bounds:
    x, y, r, b = bounds
    if r <= x or b-y < 8:
        return bounds
    gray = np.asarray(cv2.cvtColor(frame[y:b, x:r], cv2.COLOR_BGR2GRAY), dtype=np.uint8)
    h, w = gray.shape
    background = float(np.median(gray))
    threshold = min(SETTINGS.maximum_dark_level, background-SETTINGS.minimum_contrast)
    rows = np.mean(gray < threshold, axis=1) >= SETTINGS.minimum_row_coverage
    handle_threshold = min(SETTINGS.scrollbar_maximum_dark_level,
                           background-SETTINGS.scrollbar_minimum_contrast)
    handles = np.mean(gray < handle_threshold, axis=1) >= SETTINGS.scrollbar_row_coverage
    transitions = np.diff(np.r_[False, rows, False].astype(np.int8))
    starts, ends = np.flatnonzero(transitions == 1), np.flatnonzero(transitions == -1)
    edge = max(1, round(h*SETTINGS.search_fraction))
    padding = max(1, round(min(w, h)*SETTINGS.padding_fraction))
    top, bottom = 0, h
    for start, end in zip(starts, ends, strict=True):
        connected_matte = ((start == 0 and end <= edge)
                           or (end == h and start >= h-edge))
        # A zoom-out exposes the workspace right against the crop boundary.
        # It can be thicker than a toolbar, unlike an isolated dark line inside
        # the image. The edge search limit still bounds how much can be removed.
        if not connected_matte and end-start > h*SETTINGS.maximum_band_fraction:
            continue
        if end <= edge:
            top = max(top, int(end)+padding)
        if start >= h-edge:
            # A shorter grey scrollbar can sit just above the full-width black
            # window edge. Exclude it only beside an already confirmed border.
            near = max(0, int(start)-round(min(w, h)*SETTINGS.scrollbar_proximity_fraction))
            handle = np.flatnonzero(handles[near:start])
            boundary = near+int(handle[0]) if len(handle) else int(start)
            bottom = min(bottom, boundary-padding)
    return (x, y+top, r, y+bottom) if bottom > top else bounds
