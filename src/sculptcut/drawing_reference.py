"""Register Photoshop canvas views to one complete drawing, excluding application chrome."""
from dataclasses import dataclass

import cv2
import numpy as np
from numpy.typing import NDArray

from sculptcut.domain import CameraPoint, Roi, SubjectCamera
from sculptcut.roi import pixel_bounds


@dataclass(frozen=True)
class ReferenceSettings:
    features: int = 1500
    match_ratio: float = .72
    minimum_inliers: int = 8
    minimum_inlier_fraction: float = .4
    reprojection_pixels: float = 3
    minimum_scale: float = .2
    maximum_scale: float = 5
    maximum_rotation_degrees: float = 2
    canvas_inset: float = .015
    minimum_feature_spread: float = .025
    reference_edge_margin: float = .01
    clipped_edge_penalty: float = 10
    minimum_tracking_fraction: float = .5
    maximum_center_offset: float = 32
    stationary_translation_fraction: float = .005
    stationary_scale_ratio: float = 1.01
    maximum_whole_subject_aspect_ratio: float = 1.2
    source_anchors: int = 4
    anchor_scale_ratio: float = 1.2
    anchor_translation_fraction: float = .15


SETTINGS = ReferenceSettings()


class DrawingReference:
    def __init__(self, frame: NDArray[np.uint8], viewport: Roi,
                 camera: SubjectCamera) -> None:
        self.height, self.width = frame.shape[:2]
        x, y, r, b = pixel_bounds(viewport, self.width, self.height)
        inset = max(2, round(min(r-x, b-y)*SETTINGS.canvas_inset))
        inset = min(inset, (r-x-1)//2, (b-y-1)//2)
        edges = np.concatenate((frame[y+inset:b-inset, x+inset],
                                frame[y+inset:b-inset, r-inset-1]))
        colour = tuple(int(v) for v in np.median(edges, axis=0)[::-1])
        self.camera = camera.model_copy(update={'background_rgb': colour})
        self.detector = cv2.SIFT_create(nfeatures=SETTINGS.features)  # type: ignore[attr-defined]
        self.keys, self.descriptors = self.features(frame, viewport)
        self.last_score = 0.0

    def features(self, frame: NDArray[np.uint8], viewport: Roi
                 ) -> tuple[tuple[cv2.KeyPoint, ...], NDArray[np.float32] | None]:
        h, w = frame.shape[:2]
        x, y, r, b = pixel_bounds(viewport, w, h)
        inset = max(2, round(min(r-x, b-y)*SETTINGS.canvas_inset))
        mask = np.zeros((h, w), np.uint8)
        mask[y+inset:b-inset, x+inset:r-inset] = 255
        keys, descriptors = self.detector.detectAndCompute(
            cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), mask)
        return tuple(keys), (None if descriptors is None else
                             np.asarray(descriptors, dtype=np.float32))

    def point(self, frame: NDArray[np.uint8], viewport: Roi, time_us: int,
              features: tuple[tuple[cv2.KeyPoint, ...], NDArray[np.float32] | None] | None = None
              ) -> CameraPoint | None:
        self.last_score = 0.0
        keys, descriptors = self.features(frame, viewport) if features is None else features
        if self.descriptors is None or descriptors is None or len(descriptors) < 2:
            return None
        pairs = cv2.BFMatcher().knnMatch(self.descriptors, descriptors, k=2)
        matches = [a for pair in pairs if len(pair) == 2
                   for a, b in [pair] if a.distance < SETTINGS.match_ratio*b.distance]
        if len(matches) < SETTINGS.minimum_inliers:
            return None
        src = np.asarray([self.keys[m.queryIdx].pt for m in matches], dtype=np.float32)
        dst = np.asarray([keys[m.trainIdx].pt for m in matches], dtype=np.float32)
        cv2.setRNGSeed(0)
        matrix, mask = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC,
            ransacReprojThreshold=SETTINGS.reprojection_pixels)
        if matrix is None or mask is None:
            return None
        valid = mask.ravel().astype(bool)
        if (valid.sum() < SETTINGS.minimum_inliers
                or valid.mean() < SETTINGS.minimum_inlier_fraction):
            return None
        # Reject a tiny cluster such as a cursor or selection handle.
        spread = np.ptp(src[valid], axis=0)/[self.width, self.height]
        if max(spread) < SETTINGS.minimum_feature_spread:
            return None
        scale = float(np.hypot(matrix[0, 0], matrix[1, 0]))
        angle = float(np.degrees(np.arctan2(matrix[1, 0], matrix[0, 0])))
        if not SETTINGS.minimum_scale <= scale <= SETTINGS.maximum_scale:
            return None
        if abs(angle) > SETTINGS.maximum_rotation_degrees:
            return None
        p = self.camera.points[0]
        center = matrix @ np.array([p.x*self.width, p.y*self.height, 1])
        h, w = frame.shape[:2]
        x, y = float(center[0]/w), float(center[1]/h)
        if max(abs(x), abs(y)) > SETTINGS.maximum_center_offset:
            return None
        base_x, base_y = p.x*self.width/w, p.y*self.height/h
        base_w, base_h = self.camera.width*self.width/w, self.camera.height*self.height/h
        if (max(scale, 1/scale) <= SETTINGS.stationary_scale_ratio
                and abs(x-base_x)/base_w <= SETTINGS.stationary_translation_fraction
                and abs(y-base_y)/base_h <= SETTINGS.stationary_translation_fraction):
            x, y, scale = base_x, base_y, 1.0
        self.last_score = float(valid.sum()*valid.mean())
        return CameraPoint(time_us=time_us, x=x, y=y,
            width=self.camera.width*scale*self.width/w,
            height=self.camera.height*scale*self.height/h)


def locked_reference_points(points: list[CameraPoint | None], times: list[int]
                            ) -> list[CameraPoint]:
    """No invented pan/zoom ramps through gaps; suppress subpixel registration jitter."""
    valid = [i for i, p in enumerate(points) if p is not None]
    if not valid:
        return []
    filled = [points[min(valid, key=lambda j: abs(j-i))] for i in range(len(times))]
    reliable = [p for p in filled if p is not None]
    values = np.array([[p.x, p.y, p.width, p.height] for p in reliable], dtype=float)
    median = np.median(values, axis=0)
    if (np.ptp(values[:, 0])/median[2] <= SETTINGS.stationary_translation_fraction
            and np.ptp(values[:, 1])/median[3] <= SETTINGS.stationary_translation_fraction
            and np.max(values[:, 2])/np.min(values[:, 2]) <= SETTINGS.stationary_scale_ratio
            and np.max(values[:, 3])/np.min(values[:, 3]) <= SETTINGS.stationary_scale_ratio):
        values[:] = median
    return [CameraPoint(time_us=t, x=float(v[0]), y=float(v[1]),
                        width=float(v[2]), height=float(v[3]))
            for t, v in zip(times, values, strict=True)]
