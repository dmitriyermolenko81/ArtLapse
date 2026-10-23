"""Read-only input checks and bounded storage estimates before interactive cropping."""

import math
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from sculptcut.domain import ProjectSettings
from sculptcut.intro import active_intro_image, body_frame_budget
from sculptcut.media import Cancelled, MediaError, MediaInfo, discover, probe, run_process
from sculptcut.music_library import audio_duration, library_files
from sculptcut.project import fingerprint, load_project
from sculptcut.shorts_layout import MUSIC_SHORTS_PRESETS, SHORTS_PRESETS


@dataclass(frozen=True)
class PreflightPolicy:
    reserve_bytes: int = 256 * 1024 * 1024
    output_bytes_per_pixel_frame: float = 0.03
    output_copies: int = 4
    proxy_bytes_per_pixel_frame: float = 0.02
    proxy_copies: int = 2
    music_bytes_per_second: int = 48000 * 2 * 4 * 3


DEFAULT_POLICY = PreflightPolicy()


@dataclass(frozen=True)
class PreflightReport:
    required_bytes: int
    free_bytes: int
    source_count: int
    music_count: int


def cancelled(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled("Проверка отменена")


def readable(path: Path, label: str) -> None:
    try:
        with path.open("rb") as stream:
            if not stream.read(1):
                raise ValueError(f"{label}: пустой файл — {path.name}")
    except OSError as exc:
        raise ValueError(f"{label}: файл недоступен для чтения — {path}") from exc


def writable_directory(path: Path) -> Path:
    """Probe an existing ancestor without creating the requested project tree."""
    existing = path
    while not existing.exists():
        if existing.parent == existing:
            raise ValueError(f"Папка проекта недоступна: {path}")
        existing = existing.parent
    if not existing.is_dir():
        raise ValueError(f"Вместо папки обнаружен файл: {existing}")
    try:
        with tempfile.TemporaryFile(prefix=".sculptcut-write-check-", dir=existing) as stream:
            stream.write(b"ArtLapse")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise ValueError(f"Нет доступа для записи в папку: {existing}") from exc
    return existing


def estimate_space(
    sources: list[MediaInfo], settings: ProjectSettings, policy: PreflightPolicy,
) -> int:
    if settings.preset in SHORTS_PRESETS:
        width, height = 1080, 1920
    else:
        largest = max(sources, key=lambda s: s.width * s.height)
        width, height = largest.width, largest.height
    seconds = settings.target_seconds
    output = (width * height * settings.output_fps * seconds
              * policy.output_bytes_per_pixel_frame * policy.output_copies)
    proxies = 0.0
    if settings.preset not in (
        "speed", "photoshop_shorts", "photoshop_beat_shorts", "photoshop_music_shorts",
    ):
        for source in sources:
            w, h = source.width, source.height
            if source.rotation % 180 == 90:
                w, h = h, w
            proxy_w = settings.proxy_width
            proxy_h = max(2, round(h * proxy_w / w))
            proxies += (proxy_w * proxy_h * settings.proxy_fps
                        * (source.duration_us or 0) / 1e6
                        * policy.proxy_bytes_per_pixel_frame * policy.proxy_copies)
    music = (policy.music_bytes_per_second * seconds
             if settings.music_folder or settings.music_path else 0)
    return math.ceil(policy.reserve_bytes + output + proxies + music)


def check_creation(
    root: Path, paths: list[Path], settings: ProjectSettings,
    cancel: Event | None = None, stage: Callable[[str], None] | None = None,
    policy: PreflightPolicy = DEFAULT_POLICY,
) -> PreflightReport:
    """Validate candidates without writing project.json, caches, or media files."""
    cancelled(cancel)
    if not paths:
        raise ValueError("Добавьте хотя бы одно исходное видео.")
    body_frame_budget(settings, settings.output_fps, settings.target_seconds)
    root = root.resolve()
    if root.exists() and not root.is_dir():
        raise ValueError("Вместо папки проекта выбран файл.")
    if root.is_dir() and any(root.iterdir()) and not (root / "project.json").is_file():
        raise ValueError("Выберите пустую папку или папку существующего проекта.")
    existing = load_project(root) if (root / "project.json").is_file() else None
    if stage:
        stage("Проверяем видео и доступ к файлам")
    run_process([discover("ffmpeg"), "-version"], cancel)
    known = {s.path.resolve(): s for s in existing.sources} if existing else {}
    sources = []
    for path in paths:
        cancelled(cancel)
        path = path.resolve()
        readable(path, "Исходное видео")
        source = known.get(path)
        if source and fingerprint(path) != (source.location_fingerprint or source.fingerprint):
            raise ValueError(f"Исходное видео изменилось: {path.name}. Требуется повторный импорт.")
        try:
            info = probe(path, cancel)
        except Cancelled:
            raise
        except MediaError as exc:
            raise ValueError(f"Не удалось прочитать видео: {path.name}. Проверьте файл.") from exc
        if not info.duration_us:
            raise ValueError(f"Неизвестная длительность видео: {path.name}")
        sources.append(info)
    if settings.preset in MUSIC_SHORTS_PRESETS:
        if not settings.music_folder and not settings.music_path:
            raise ValueError("Для шортса под музыку выберите файл или папку с музыкой.")
    if stage:
        stage("Проверяем музыку")
    tracks = (library_files(settings.music_folder) if settings.music_folder
              else [settings.music_path] if settings.music_path else [])
    for track in tracks:
        cancelled(cancel)
        readable(track, "Музыка")
        try:
            audio_duration(track, cancel)
        except Cancelled:
            raise
        except MediaError as exc:
            raise ValueError(f"Не удалось прочитать музыку: {track.name}. Проверьте файл.") from exc
    if (image := active_intro_image(settings)) is not None:
        readable(image, "Заставка")
    if settings.presentation_video:
        if stage:
            stage("Проверяем видео-презентацию")
        readable(settings.presentation_video, "Видео-презентация")
        presentation = probe(settings.presentation_video, cancel)
        if not presentation.duration_us:
            raise ValueError("Неизвестная длительность видео-презентации")
    if stage:
        stage("Проверяем папки и свободное место")
    directories = [root, *(root / name for name in (
        "proxy", "thumbnails", "previews", "renders", "temp", "logs", "music",
    ))]
    volumes: dict[int, Path] = {}
    for directory in directories:
        cancelled(cancel)
        parent = writable_directory(directory)
        volumes[parent.stat().st_dev] = parent
    for name in ("project.json", "analysis.sqlite", "render_plan.json"):
        path = root / name
        if path.exists():
            try:
                fd = os.open(path, os.O_WRONLY)
                os.close(fd)
            except OSError as exc:
                raise ValueError(f"Файл проекта недоступен для записи: {path.name}") from exc
    required = estimate_space(sources, settings, policy)
    free = min(shutil.disk_usage(parent).free for parent in volumes.values())
    if free < required:
        gib = 1024 ** 3
        raise ValueError(
            f"Недостаточно свободного места: доступно {free / gib:.2f} ГБ, "
            f"нужно примерно {required / gib:.2f} ГБ с запасом на временные файлы."
        )
    cancelled(cancel)
    return PreflightReport(required, free, len(sources), len(tracks))
