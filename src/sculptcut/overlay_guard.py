"""Conservative visual detection of central outlined alerts and popup text."""

from dataclasses import dataclass

import cv2
import numpy as np

from sculptcut.analysis import Gray


@dataclass(frozen=True)
class OverlaySettings:
    width: int = 960
    white: int = 220
    minimum_area: float = 0.008
    maximum_area: float = 0.28
    minimum_height: float = 0.12
    minimum_width: float = 0.06
    text_rows: int = 2
    letters_per_row: int = 5
    text_search_height: float = 0.23
    padding_seconds: float = 1.0


SETTINGS = OverlaySettings()


def _text_rows(region: Gray) -> int:
    edges = cv2.Canny(region, 40, 100)
    _, _, stats, centers = cv2.connectedComponentsWithStats(edges)
    points = sorted((float(c[1]), int(s[0]), int(s[2]))
                    for s, c in zip(stats[1:], centers[1:], strict=True)
                    if 2 <= s[2] <= 24 and 4 <= s[3] <= 22 and s[4] >= 5)
    rows: list[list[tuple[float, int, int]]] = []
    for point in points:
        if not rows or point[0] - rows[-1][0][0] > 5:
            rows.append([point])
        else:
            rows[-1].append(point)
    return sum(len(row) >= SETTINGS.letters_per_row and
               max(x+w for _, x, w in row)-min(x for _, x, _ in row) >= 40 for row in rows)


def notification_overlay(gray: Gray) -> bool:
    h, w = gray.shape
    image = cv2.resize(gray, (SETTINGS.width, max(1, round(h*SETTINGS.width/w)))).astype(np.uint8)
    h, w = image.shape
    # Side toolbars and the navigation head are ordinary application UI.
    region = image[round(h*0.15):round(h*0.9), round(w*0.18):round(w*0.82)]
    mask = (region >= SETTINGS.white).astype(np.uint8)*255
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        area = cv2.contourArea(contour)
        x, y, cw, ch = cv2.boundingRect(contour)
        if not (region.size*SETTINGS.minimum_area <= area <= region.size*SETTINGS.maximum_area
                and ch >= region.shape[0]*SETTINGS.minimum_height
                and cw >= region.shape[1]*SETTINGS.minimum_width):
            continue
        below = region[y+ch:min(region.shape[0], y+ch+round(h*SETTINGS.text_search_height)),
                       max(0, x-cw//2):min(region.shape[1], x+cw+cw//2)]
        if below.size and _text_rows(below) >= SETTINGS.text_rows:
            return True
        # Filled notification rectangles may contain their own text.
        if area/(cw*ch) > 0.85 and _text_rows(region[y:y+ch, x:x+cw]) >= SETTINGS.text_rows:
            return True
    edges = cv2.Canny(region, 40, 100)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        x, y, cw, ch = cv2.boundingRect(contour)
        if not (region.size*0.02 < cw*ch < region.size*0.5
                and cw > region.shape[1]*0.15 and ch > region.shape[0]*0.06):
            continue
        polygon = cv2.approxPolyDP(contour, 0.02*cv2.arcLength(contour, True), True)
        if (len(polygon) == 4 and cv2.contourArea(contour)/(cw*ch) > 0.85
                and _text_rows(region[y+2:y+ch-2, x+2:x+cw-2]) >= SETTINGS.text_rows):
            return True
    return False
