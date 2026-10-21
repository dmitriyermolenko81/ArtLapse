"""Sequential CPU features, transactional batches and resumable extraction."""

import csv
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from threading import Event

import cv2
import numpy as np
from numpy.typing import NDArray

from sculptcut.domain import Model, Project, Roi, SourceMedia
from sculptcut.media import Cancelled, MediaError
from sculptcut.project import cache_get, cache_key, cache_put, database, validate_sources
from sculptcut.proxy import make_proxies, proxy_path
from sculptcut.roi import masks
from sculptcut.webcam import AnalysisCapture, analysis_media_key, webcam_roi

FEATURE_VERSION = 2
Gray = NDArray[np.uint8]


def brightness_histogram(values: Gray) -> NDArray[np.float64]:
    """Exact 32-bin histogram for uint8 pixels, without generic float binning."""
    counts = np.bincount((values >> 3).ravel(), minlength=32).astype(np.float64)
    normalized: NDArray[np.float64] = counts / max(1, counts.sum())
    return normalized


class FrameFeature(Model):
    source_id: str
    timestamp_us: int
    brightness: float
    contrast: float
    roi_change: float
    ui_change: float
    edge_change: float
    histogram_change: float
    changed_pixel_ratio: float
    signed_change: float = 0
    noise: float
    perceptual_hash: str
    measurement_quality: float
    camera_score: float = 0
    aligned_residual: float = 0


def analysis_key(project: Project, source: SourceMedia) -> str:
    roi = project.rois.get(source.id)
    return cache_key(
        FEATURE_VERSION, analysis_media_key(project, source), roi.model_dump() if roi else "full"
    )


def measure(
    current: Gray,
    previous: Gray,
    roi: Roi,
    source_id: str,
    timestamp: int,
    edge_pair: tuple[Gray, Gray] | None = None,
    histogram_pair: tuple[NDArray[np.float64], NDArray[np.float64]] | None = None,
    mask_pair: tuple[NDArray[np.bool_], NDArray[np.bool_]] | None = None,
) -> FrameFeature:
    canvas, ui = mask_pair if mask_pair is not None else masks(
        roi, current.shape[1], current.shape[0]
    )
    current_values = current[canvas]
    previous_values = previous[canvas]
    delta = cv2.absdiff(current, previous).astype(np.uint8)
    pixels = delta[canvas].astype(np.float32)
    median = float(np.median(pixels))
    noise = float(np.median(np.abs(pixels - median)))
    threshold = max(4.0, min(12.0, median + 3 * noise))
    current_edges, previous_edges = (
        edge_pair
        if edge_pair is not None
        else (
            cv2.Canny(current, 50, 150).astype(np.uint8, copy=False),
            cv2.Canny(previous, 50, 150).astype(np.uint8, copy=False),
        )
    )
    edges = cv2.absdiff(current_edges, previous_edges).astype(np.uint8, copy=False)
    hist, old_hist = histogram_pair if histogram_pair is not None else (
        brightness_histogram(current_values), brightness_histogram(previous_values)
    )
    small = cv2.resize(current, (32, 32)).astype(np.float32)
    dct = cv2.dct(small).astype(np.float32)[:8, :8].flatten()[1:]
    bits = dct > np.median(dct)
    phash = hex(int("".join("1" if b else "0" for b in bits), 2))[2:]
    return FrameFeature(
        source_id=source_id,
        timestamp_us=timestamp,
        brightness=float(np.mean(current_values)) / 255,
        contrast=float(np.std(current_values)) / 255,
        roi_change=float(np.mean(pixels)) / 255,
        ui_change=float(np.mean(delta[ui])) / 255 if ui.any() else 0,
        edge_change=float(np.mean(edges[canvas])) / 255,
        histogram_change=float(np.abs(hist - old_hist).sum()) / 2,
        changed_pixel_ratio=float(np.mean(pixels > threshold)),
        noise=noise / 255,
        signed_change=float(np.mean(current_values.astype(float) - previous_values)) / 255,
        perceptual_hash=phash,
        measurement_quality=1.0,
    )


def extract_features(
    root: Path,
    project: Project,
    cancel: Event | None = None,
    progress: Callable[[int], None] | None = None,
) -> None:
    validate_sources(root, project)
    make_proxies(root, project, cancel)
    connection = database(root)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS features "
            "(source_id TEXT,key TEXT,timestamp INTEGER,value TEXT,"
            "PRIMARY KEY(source_id,key,timestamp))"
        )
        connection.commit()
        for source in project.sources:
            key = analysis_key(project, source)
            if cache_get(root, "features", source.id, key):
                if progress:
                    progress(source.global_start_us + source.duration_us)
                continue
            row = connection.execute(
                "SELECT position FROM checkpoints WHERE stage='features' AND source_id=? AND key=?",
                (source.id, key),
            ).fetchone()
            completed = int(row[0]) if row else -1
            roi = project.rois.get(source.id, Roi(x=0, y=0, width=1, height=1))
            capture = AnalysisCapture(
                proxy_path(root, project, source), webcam_roi(project.settings, source.id)
            )
            previous: Gray | None = None
            previous_edges: Gray | None = None
            previous_histogram: NDArray[np.float64] | None = None
            mask_pair = None
            index = max(0, round(completed * project.settings.proxy_fps / 1_000_000))
            if index:
                capture.set(cv2.CAP_PROP_POS_FRAMES, index)
            pending = 0
            last_timestamp = -1
            try:
                while True:
                    if cancel is not None and cancel.is_set():
                        connection.commit()
                        raise Cancelled("Feature extraction cancelled; committed batches retained")
                    ok, frame = capture.read()
                    if not ok:
                        break
                    timestamp = round(float(capture.get(cv2.CAP_PROP_POS_MSEC)) * 1000)
                    expected = round(index * 1_000_000 / project.settings.proxy_fps)
                    if abs(timestamp - expected) > 2000 or timestamp <= last_timestamp:
                        raise MediaError("Proxy presentation timestamp mismatch")
                    last_timestamp = timestamp
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.uint8)
                    if previous is None:
                        previous = gray
                    if timestamp > completed and timestamp < source.duration_us:
                        if mask_pair is None:
                            mask_pair = masks(roi, gray.shape[1], gray.shape[0])
                        current_histogram = brightness_histogram(gray[mask_pair[0]])
                        if previous_histogram is None:
                            previous_histogram = brightness_histogram(previous[mask_pair[0]])
                        current_edges = cv2.Canny(gray, 50, 150).astype(np.uint8, copy=False)
                        if previous_edges is None:
                            previous_edges = cv2.Canny(previous, 50, 150).astype(
                                np.uint8, copy=False
                            )
                        feature = measure(
                            gray,
                            previous,
                            roi,
                            source.id,
                            timestamp,
                            (current_edges, previous_edges),
                            (current_histogram, previous_histogram),
                            mask_pair,
                        )
                        previous_edges = current_edges
                        previous_histogram = current_histogram
                        connection.execute(
                            "INSERT OR REPLACE INTO features VALUES(?,?,?,?)",
                            (source.id, key, timestamp, feature.model_dump_json()),
                        )
                        connection.execute(
                            "INSERT OR REPLACE INTO checkpoints VALUES(?,?,?,?)",
                            ("features", source.id, key, timestamp),
                        )
                        pending += 1
                        if timestamp % 60_000_000 == 0:
                            encoded, png = cv2.imencode(".png", frame)
                            if encoded:
                                png.tofile(
                                    str(root / "thumbnails" / f"{source.id}-{timestamp}.png")
                                )
                        if pending >= 100:
                            connection.commit()
                            pending = 0
                        if progress:
                            progress(source.global_start_us + timestamp)
                    previous = gray
                    index += 1
                connection.commit()
                if index == 0:
                    raise MediaError("Proxy contains no readable frames")
                cache_put(
                    root, "features", source.id, key, {"frames": index, "last_us": last_timestamp}
                )
            finally:
                capture.release()
    finally:
        connection.close()
    export_features(root, project)


def read_features(
    root: Path,
    project: Project,
    source: SourceMedia,
    start_us: int = 0,
    end_us: int = 2**63 - 1,
) -> Iterator[FrameFeature]:
    connection = database(root)
    try:
        for row in connection.execute(
            "SELECT value FROM features WHERE source_id=? AND key=? "
            "AND timestamp>=? AND timestamp<? ORDER BY timestamp",
            (source.id, analysis_key(project, source), start_us, end_us),
        ):
            yield FrameFeature.model_validate_json(row[0])
    finally:
        connection.close()


def export_features(root: Path, project: Project) -> None:
    with (
        (root / "logs" / "features.jsonl").open("w", encoding="utf-8") as lines,
        (root / "logs" / "features.csv").open("w", encoding="utf-8", newline="") as table,
    ):
        writer = csv.DictWriter(table, fieldnames=list(FrameFeature.model_fields))
        writer.writeheader()
        for source in project.sources:
            for feature in read_features(root, project, source):
                lines.write(json.dumps(feature.model_dump()) + "\n")
                writer.writerow(feature.model_dump())
