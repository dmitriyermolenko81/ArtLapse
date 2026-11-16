"""Find an enclosed drawing sheet against a neutral application workspace."""

from dataclasses import dataclass

import cv2
import numpy as np
from numpy.typing import NDArray

from sculptcut.domain import CameraPoint, Roi


@dataclass(frozen=True)
class PageSettings:
    minimum_area: float = .02
    maximum_area: float = .9
    minimum_fill: float = .9
    colour_distance: float = 18
    bright_level: int = 185
    workspace_chroma: float = 16
    workspace_brightness: int = 155
    workspace_fraction: float = .8
    ring_pixels: int = 5
    minimum_tracking_fraction: float = .8
    maximum_subject_fraction: float = .2
    maximum_subject_coverage: float = .8
    maximum_aspect_ratio_change: float = 1.15
    framing_margin: float = 1.0
    stationary_scale_ratio: float = 1.015
    stationary_center_fraction: float = .005
    sketch_delta: int = 5
    sketch_dilation: int = 5
    sketch_minimum_height: float = .25


SETTINGS = PageSettings()


def page_subject(frame: NDArray[np.uint8], page: Roi) -> Roi | None:
    """Find a faint complete sketch inside an already enclosed sheet."""
    h, w = frame.shape[:2]
    x, y = round(page.x*w), round(page.y*h)
    r, b = round((page.x+page.width)*w), round((page.y+page.height)*h)
    gray = cv2.cvtColor(frame[y:b, x:r], cv2.COLOR_BGR2GRAY).astype(np.uint8)
    colour = np.median(frame[y:b, x:r].reshape(-1, 3), axis=0)
    if np.ptp(colour) > SETTINGS.workspace_chroma or np.median(gray) < SETTINGS.bright_level:
        return None
    inset = max(2, SETTINGS.sketch_dilation)
    if min(gray.shape) <= inset*2:
        return None
    mask = (np.abs(gray.astype(np.float32)-float(np.median(gray))) > SETTINGS.sketch_delta
            ).astype(np.uint8)
    mask[:inset] = mask[-inset:] = 0
    mask[:, :inset] = mask[:, -inset:] = 0
    mask = cv2.dilate(mask, np.ones((SETTINGS.sketch_dilation,)*2, np.uint8))
    _, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    parts = [s for s in stats[1:] if s[3] >= gray.shape[0]*SETTINGS.sketch_minimum_height
             and s[2]*s[3] < gray.size*SETTINGS.maximum_subject_coverage]
    if not parts:
        return None
    bx, by, bw, bh, _ = max(parts, key=lambda s:int(s[4]))
    return Roi(x=float((x+bx)/w), y=float((y+by)/h), width=float(bw/w), height=float(bh/h))


def lock_page_points(points: list[CameraPoint]) -> list[CameraPoint]:
    """Share near-identical sheet geometry across episodes, retaining real pans/zooms."""
    if not points:
        return []
    result = list(points)
    groups: list[list[int]] = []
    for i in sorted(range(len(points)), key=lambda i: points[i].width or 0):
        width = points[i].width
        assert width is not None
        if not groups or width/(points[groups[-1][0]].width or width) > (
            SETTINGS.stationary_scale_ratio
        ):
            groups.append([])
        groups[-1].append(i)
    for group in groups:
        width = float(np.median([points[i].width for i in group]))
        for i in group:
            p = result[i]
            assert p.width is not None and p.height is not None
            result[i] = p.model_copy(update={'width':width, 'height':p.height*width/p.width})
        for axis in ('x', 'y'):
            clusters: list[list[int]] = []
            for i in sorted(group, key=lambda i: getattr(points[i], axis)):
                value = getattr(points[i], axis)
                if not clusters or value-getattr(points[clusters[-1][0]], axis) > (
                    width*SETTINGS.stationary_center_fraction
                ):
                    clusters.append([])
                clusters[-1].append(i)
            for cluster in clusters:
                value = float(np.median([getattr(points[i], axis) for i in cluster]))
                for i in cluster:
                    result[i] = result[i].model_copy(update={axis:value})
    return result


def enclosed_page(frame: NDArray[np.uint8], *, rotated: bool = False) -> Roi | None:
    h, w = frame.shape[:2]
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    chroma = np.linalg.norm(lab[:, :, 1:].astype(np.float32)-128, axis=2)
    paper = ((chroma > SETTINGS.colour_distance)
             | (lab[:, :, 0] > SETTINGS.bright_level)).astype(np.uint8)
    contours, _ = cv2.findContours(paper, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    neutral = ((chroma < SETTINGS.workspace_chroma)
               & (lab[:, :, 0] < SETTINGS.workspace_brightness))
    candidates = []
    ring = SETTINGS.ring_pixels
    for contour in contours:
        x, y, cw, ch = cv2.boundingRect(contour)
        area = cw*ch
        if not SETTINGS.minimum_area <= area/(w*h) <= SETTINGS.maximum_area:
            continue
        if x < ring or y < ring or x+cw+ring > w or y+ch+ring > h:
            continue
        rectangle = cv2.minAreaRect(contour)[1] if rotated else (cw,ch)
        rectangle_area = max(1.0, float(rectangle[0]*rectangle[1]))
        if cv2.contourArea(contour)/rectangle_area < SETTINGS.minimum_fill:
            continue
        surrounding = np.concatenate((neutral[y-ring:y, x:x+cw].ravel(),
            neutral[y+ch:y+ch+ring, x:x+cw].ravel(),
            neutral[y:y+ch, x-ring:x].ravel(), neutral[y:y+ch, x+cw:x+cw+ring].ravel()))
        if float(surrounding.mean()) < SETTINGS.workspace_fraction:
            continue
        distance = float(np.hypot((x+cw/2)/w-.5, (y+ch/2)/h-.5))
        candidates.append((area/(1+distance), Roi(x=x/w, y=y/h, width=cw/w, height=ch/h)))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def page_colour(frame: NDArray[np.uint8], page: Roi, *,
                rotated: bool = False) -> tuple[int, int, int]:
    h, w = frame.shape[:2]
    x, y = round(page.x*w)+2, round(page.y*h)+2
    r, b = round((page.x+page.width)*w)-2, round((page.y+page.height)*h)-2
    pixels = np.concatenate((frame[y:b, x], frame[y:b, r-1], frame[y, x:r]))
    if rotated:
        # A rotated sheet's axis-aligned perimeter also contains workspace.
        region = frame[y:b,x:r]
        lab = cv2.cvtColor(region,cv2.COLOR_BGR2LAB)
        paper = ((np.linalg.norm(lab[:,:,1:].astype(np.float32)-128,axis=2)
                  > SETTINGS.colour_distance) | (lab[:,:,0] > SETTINGS.bright_level))
        if np.any(paper):
            pixels = region[paper]
    colour = np.median(pixels, axis=0)
    return int(colour[2]), int(colour[1]), int(colour[0])
