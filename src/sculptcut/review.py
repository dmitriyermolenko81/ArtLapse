"""Visual-return candidates are review evidence, never automatic undo deletions."""

import json
from collections import deque
from collections.abc import Iterable
from html import escape
from pathlib import Path
from threading import Event

import cv2

from sculptcut.analysis import FrameFeature, analysis_key, read_features
from sculptcut.domain import Model, Project
from sculptcut.i18n import language, tr
from sculptcut.localization import LABELS
from sculptcut.media import Cancelled
from sculptcut.project import atomic_write, cache_key
from sculptcut.proxy import proxy_path
from sculptcut.time_format import format_time


class ReviewCandidate(Model):
    source_id: str
    start_us: int
    end_us: int
    reason: str
    action: str = "REVIEW"


def visual_returns(features: Iterable[FrameFeature]) -> list[ReviewCandidate]:
    history: deque[FrameFeature] = deque(maxlen=120)
    result = []
    last_sample = -1_000_000
    last_end = -1
    for feature in features:
        if feature.timestamp_us - last_sample < 1_000_000:
            continue
        last_sample = feature.timestamp_us
        stable = (
            feature.roi_change < 0.002
            and feature.ui_change < 0.002
            and feature.contrast > 0.03
            and feature.histogram_change < 0.1
            and feature.measurement_quality > 0.9
        )
        if stable and feature.timestamp_us > last_end + 2_000_000:
            for index, start in enumerate(history):
                if feature.timestamp_us - start.timestamp_us < 3_000_000:
                    continue
                if (
                    start.roi_change >= 0.002
                    or start.histogram_change >= 0.1
                    or start.ui_change >= 0.002
                ):
                    continue
                similarity = (
                    int(start.perceptual_hash, 16) ^ int(feature.perceptual_hash, 16)
                ).bit_count()
                middle = list(history)[index + 1 :]
                differing = any(
                    (int(f.perceptual_hash, 16) ^ int(start.perceptual_hash, 16)).bit_count() >= 12
                    for f in middle
                )
                context_safe = all(f.histogram_change < 0.2 and f.ui_change < 0.02 for f in middle)
                if similarity <= 3 and differing and context_safe:
                    result.append(
                        ReviewCandidate(
                            source_id=feature.source_id,
                            start_us=start.timestamp_us,
                            end_us=feature.timestamp_us,
                            reason=(
                                "Similar stable view returned after a different view; "
                                "not proof of Undo"
                            ),
                        )
                    )
                    last_end = feature.timestamp_us
                    break
        history.append(feature)
    return result


def review_project(root: Path, project: Project) -> list[ReviewCandidate]:
    result = []
    for source in project.sources:
        result.extend(visual_returns(read_features(root, project, source)))
    atomic_write(root / "review.json", json.dumps([r.model_dump() for r in result], indent=2))
    return result


def review_html(root: Path, project: Project, cancel: Event | None = None) -> Path:
    if cancel and cancel.is_set():
        raise Cancelled("Review cancelled")
    segments_path = root / "segments.json"
    destination = root / "review.html"
    manifest = root / "logs" / "review-state.json"
    key = cache_key(
        "review-i18n-3", language(),
        [analysis_key(project, source) for source in project.sources],
        segments_path.read_text(encoding="utf-8") if segments_path.exists() else "",
    )
    if destination.is_file() and manifest.is_file():
        try:
            state = json.loads(manifest.read_text(encoding="utf-8"))
            if state["key"] == key and all((root / p).is_file() for p in state["images"]):
                return destination
        except (ValueError, OSError, KeyError, TypeError):
            pass
    candidates = review_project(root, project)
    if segments_path.exists():
        for segment in json.loads(segments_path.read_text(encoding="utf-8")):
            if segment["kind"] in ("UNKNOWN", "MIXED"):
                candidates.append(
                    ReviewCandidate(
                        source_id=segment["source_id"],
                        start_us=segment["start_us"],
                        end_us=segment["end_us"],
                        reason=f"Uncertain {segment['kind']} activity; preserved by default",
                    )
                )
    sources = {s.id: s for s in project.sources}
    cards = []
    image_files = []
    for index, candidate in enumerate(candidates):
        if cancel and cancel.is_set():
            raise Cancelled("Review cancelled")
        if candidate.reason.startswith("Uncertain "):
            kind = candidate.reason.split()[1]
            reason = tr(f"{tr(LABELS.get(kind, kind))}: фрагмент сохранён для ручной проверки.")
        elif candidate.reason.startswith("Similar stable view returned"):
            reason = tr("Возврат к похожему устойчивому виду. Это не доказывает отмену действий.")
        else:
            reason = candidate.reason
        path = proxy_path(root, project, sources[candidate.source_id]).relative_to(root).as_posix()
        start, end = candidate.start_us / 1e6, candidate.end_us / 1e6
        images = []
        capture = cv2.VideoCapture(str(root / path))
        try:
            for label, time in (("before", start), ("after", max(start, end - 0.2))):
                capture.set(cv2.CAP_PROP_POS_MSEC, time * 1000)
                ok, frame = capture.read()
                if ok:
                    ok, png = cv2.imencode(".png", frame)
                    if ok:
                        image_path = f"thumbnails/review-{index}-{label}.png"
                        png.tofile(str(root / image_path))
                        image_files.append(image_path)
                        caption = tr("До") if label == "before" else tr("После")
                        images.append(f'<img width="48%" alt="{caption}" src="{image_path}">')
        finally:
            capture.release()
        cards.append(
            f"<article><h2>{escape(candidate.source_id)} · "
            f"{format_time(start, 2)}–{format_time(end, 2)}</h2>"
            f"<p>{escape(reason)}</p>" + "".join(images) + tr('<video controls preload="none" '
            f'src="{escape(path)}#t={start},{end}"></video>'
            "<p>Выберите в приложении: сохранить, удалить или изменить скорость. "
            "Результаты анализа сохраняются.</p></article>")
        )
    atomic_write(
        destination,
        tr('<!doctype html><html lang="ru"><meta charset="utf-8">'
        "<title>ArtLapse — проверка фрагментов</title>"
        "<style>body{background:#15171b;color:#eee;font:16px system-ui;"
        "max-width:1000px;margin:40px auto}"
        "article{padding:24px;margin:20px 0;background:#20232a;"
        "border-radius:12px}video{width:100%}</style>"
        "<h1>Проверка сомнительных фрагментов</h1>")
        + ("".join(cards) or tr("<p>Фрагментов для проверки нет.</p>"))
        + "</html>",
    )
    atomic_write(manifest, json.dumps({"key": key, "images": image_files}))
    return destination
