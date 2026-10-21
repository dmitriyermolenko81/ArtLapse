"""Observable 2D motion evidence; no claim of explaining arbitrary 3D rotation."""

from pathlib import Path
from threading import Event

import cv2
import numpy as np

from sculptcut.analysis import Gray, analysis_key
from sculptcut.domain import Model, Project
from sculptcut.media import Cancelled
from sculptcut.project import cache_get, cache_key, cache_put, database
from sculptcut.proxy import proxy_path
from sculptcut.roi import pixel_bounds
from sculptcut.webcam import AnalysisCapture, webcam_roi


class Motion(Model):
    timestamp_us: int = 0
    quality: float = 0
    coverage: float = 0
    inlier_ratio: float = 0
    displacement: float = 0
    residual: float = 1
    camera_score: float = 0
    explanation: str = "UNKNOWN: insufficient tracking"


def estimate_motion(previous: Gray, current: Gray, timestamp: int = 0) -> Motion:
    points = cv2.goodFeaturesToTrack(previous, maxCorners=300, qualityLevel=0.01, minDistance=5)
    if points is None or len(points) < 8:
        return Motion(timestamp_us=timestamp)
    tracked, status, _ = cv2.calcOpticalFlowPyrLK(previous, current, points, points.copy())
    if tracked is None or status is None:
        return Motion(timestamp_us=timestamp)
    valid = np.asarray(status, dtype=np.uint8).flatten() != 0
    before = np.asarray(points, dtype=np.float32).reshape(-1, 2)[valid]
    after = np.asarray(tracked, dtype=np.float32).reshape(-1, 2)[valid]
    if len(before) < 8:
        return Motion(timestamp_us=timestamp)
    # RANSAC RNG is explicitly seeded for reproducible single-threaded CPU analysis.
    cv2.setRNGSeed(42)
    matrix, inliers = cv2.estimateAffinePartial2D(
        before, after, method=cv2.RANSAC, ransacReprojThreshold=2, maxIters=2000
    )
    if matrix is None or inliers is None:
        return Motion(timestamp_us=timestamp)
    selected = np.asarray(inliers, dtype=np.uint8).flatten() != 0
    good = before[selected]
    if len(good) < 6:
        return Motion(timestamp_us=timestamp)
    height, width = current.shape
    hull = cv2.convexHull(good)
    coverage = float(cv2.contourArea(hull)) / (width * height)
    ratio = float(np.mean(selected))
    displacement = float(np.median(np.linalg.norm(after[selected] - good, axis=1)))
    warped = cv2.warpAffine(previous, matrix, (width, height))
    mask = cv2.warpAffine(np.full_like(previous, 255), matrix, (width, height)) > 250
    delta = cv2.absdiff(warped, current).astype(np.float32)
    residual = float(np.mean(delta[mask])) / 255 if mask.any() else 1
    quality = min(1, len(good) / 30) * min(1, coverage / 0.3) * ratio
    score = quality * max(0, 1 - residual / 0.08) if displacement > 0.5 else 0
    explanation = "UNKNOWN: insufficient spatial coverage"
    if quality >= 0.5:
        explanation = "CAMERA: coherent 2D motion" if score > 0.6 else "MIXED: unexplained residual"
        if displacement <= 0.5:
            explanation = "STATIC_OR_LOCAL: no global motion"
    return Motion(
        timestamp_us=timestamp,
        quality=quality,
        coverage=coverage,
        inlier_ratio=ratio,
        displacement=displacement,
        residual=residual,
        camera_score=score,
        explanation=explanation,
    )


def analyze_motion(root: Path, project: Project, cancel: Event | None = None) -> None:
    connection = database(root)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS motion "
            "(source_id TEXT,key TEXT,timestamp INTEGER,value TEXT,"
            "PRIMARY KEY(source_id,key,timestamp))"
        )
        connection.commit()
        for source in project.sources:
            key = cache_key("motion-2", analysis_key(project, source))
            if cache_get(root, "motion", source.id, key):
                continue
            capture = AnalysisCapture(
                proxy_path(root, project, source), webcam_roi(project.settings, source.id)
            )
            previous: Gray | None = None
            row = connection.execute(
                "SELECT MAX(timestamp) FROM motion WHERE source_id=? AND key=?",
                (source.id, key),
            ).fetchone()
            completed = int(row[0]) if row and row[0] is not None else -1
            index = max(0, round(completed * project.settings.proxy_fps / 1_000_000))
            if index:
                capture.set(cv2.CAP_PROP_POS_FRAMES, index)
            try:
                while True:
                    if cancel is not None and cancel.is_set():
                        connection.commit()
                        raise Cancelled("Motion analysis cancelled")
                    ok, frame = capture.read()
                    if not ok:
                        break
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.uint8)
                    if source.id in project.rois:
                        x, y, right, bottom = pixel_bounds(
                            project.rois[source.id], gray.shape[1], gray.shape[0]
                        )
                        gray = gray[y:bottom, x:right]
                    timestamp = round(float(capture.get(cv2.CAP_PROP_POS_MSEC)) * 1000)
                    if timestamp <= completed:
                        previous = gray
                        index += 1
                        continue
                    motion = (
                        estimate_motion(previous, gray, timestamp)
                        if previous is not None
                        else Motion(timestamp_us=timestamp)
                    )
                    connection.execute(
                        "INSERT OR REPLACE INTO motion VALUES(?,?,?,?)",
                        (source.id, key, timestamp, motion.model_dump_json()),
                    )
                    if index % 100 == 0:
                        connection.commit()
                        cv2.putText(
                            frame,
                            motion.explanation,
                            (10, 25),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.45,
                            (0, 255, 255),
                            1,
                        )
                        ok, png = cv2.imencode(".png", frame)
                        if ok:
                            png.tofile(str(root / "thumbnails" / f"{source.id}-motion-{index}.png"))
                    previous = gray
                    index += 1
                connection.commit()
            finally:
                capture.release()
            cache_put(root, "motion", source.id, key, {"frames": index})
    finally:
        connection.close()
