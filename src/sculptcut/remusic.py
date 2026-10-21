"""Replace music; rebuild musical stills or motion episodes against their snapshot."""

import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import Literal

from sculptcut.audio import add_music
from sculptcut.domain import DEFAULT_BEAT_SYNC_MODE, DEFAULT_BEAT_SYNC_STRENGTH, Project
from sculptcut.media import discover, probe, run_process
from sculptcut.music_library import soundtrack
from sculptcut.output_naming import next_variant_path
from sculptcut.project import load_project, resolve_snapshot_sources, save_project
from sculptcut.shorts_layout import MUSIC_SHORTS_PRESETS
from sculptcut.variants import cut_history, read_variant, save_variant


def replace_music(
    root: Path,
    video: Path,
    folder: Path,
    seed: int,
    cancel: Event | None,
    selection_mode: Literal["dynamic", "full_tracks"] | None = None,
    progress: Callable[[float], None] | None = None,
) -> Path:
    """Use a single audio file or a library folder; preserve the previous export."""
    project = load_project(root)
    previous = read_variant(video)
    snapshot_project = (
        Project.model_validate(previous["project"]) if previous.get("project") else None
    )
    if snapshot_project is not None:
        snapshot_project = resolve_snapshot_sources(snapshot_project, project)
    base_settings = snapshot_project.settings if snapshot_project else project.settings
    music_path = folder if folder.is_file() else None
    music_folder = None if music_path is not None else folder
    trimmed = bool(cut_history(video))
    settings = base_settings.model_copy(
        update={
            "music_folder": music_folder,
            "music_path": music_path,
            "music_seed": seed,
            "music_selection_mode": selection_mode or base_settings.music_selection_mode,
        }
    )
    if settings.preset in MUSIC_SHORTS_PRESETS:
        settings = settings.model_copy(update={
            "beat_sync_mode": DEFAULT_BEAT_SYNC_MODE,
            "beat_sync_strength": DEFAULT_BEAT_SYNC_STRENGTH,
        })
    info = probe(video)
    if not info.duration_us:
        raise ValueError("Cannot determine video duration")
    duration = info.duration_us / 1e6
    music = soundtrack(root, settings, duration, cancel)
    assert music is not None
    destination = next_variant_path(video.parent, "music-variant", settings.preset)
    rebuild = settings.preset in MUSIC_SHORTS_PRESETS and not trimmed
    if rebuild:
        from sculptcut.planning import plan_project
        from sculptcut.rendering import render

        render_project = (snapshot_project or project).model_copy(update={"settings": settings})
        plan = plan_project(root, render_project, settings.target_seconds, cancel, progress)
        render(
            root, render_project, plan, destination, cancel, progress=progress,
            variant_parent=video, variant_operation="music",
        )
    else:
        with tempfile.TemporaryDirectory(prefix="remusic-", dir=video.parent) as scratch:
            result = Path(scratch) / "result.mp4"
            add_music(
                video, music, result, duration, settings.music_loop, settings.music_fade_seconds,
                cancel,
            )
            actual = probe(result)
            if (
                not actual.audio_streams
                or not actual.duration_us
                or abs(actual.duration_us - info.duration_us) > 100000
            ):
                raise ValueError("Music replacement duration mismatch")
            run_process([
                discover("ffmpeg"), "-v", "error", "-xerror", "-i", str(result),
                "-f", "null", "-",
            ], cancel)
            os.rename(result, destination)
    save_project(root, project.model_copy(update={"settings": project.settings.model_copy(
        update={
            "music_folder": music_folder, "music_path": music_path, "music_seed": seed,
            "music_selection_mode": settings.music_selection_mode,
        },
    )}))
    if snapshot_project:
        snapshot_project = snapshot_project.model_copy(
            update={
                "settings": snapshot_project.settings.model_copy(
                    update={
                        "music_folder": music_folder,
                        "music_path": music_path,
                        "music_seed": seed,
                        "music_selection_mode": settings.music_selection_mode,
                        "music_loop": settings.music_loop,
                        "music_fade_seconds": settings.music_fade_seconds,
                    }
                )
            }
        )
    if not rebuild:
        save_variant(
            destination, project=snapshot_project, music=music, parent=video, operation="music"
        )
    if progress:
        progress(1)
    return destination
