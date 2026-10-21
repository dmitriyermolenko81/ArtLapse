"""Rebuild a finished variant with another intro, reusing its edit plan."""

import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from threading import Event

from sculptcut.domain import Project
from sculptcut.intro import intro_frames, intro_signature
from sculptcut.music_library import music_signature
from sculptcut.output_naming import next_variant_path
from sculptcut.planning import RenderPlan
from sculptcut.presentation import presentation_frames
from sculptcut.project import load_project, resolve_snapshot_sources, save_project
from sculptcut.rendering import render
from sculptcut.variants import cut_history, read_variant, save_variant


def shift_intro_history(
    history: list[list[tuple[int, int]]], prefix: int, shift: int,
) -> list[list[tuple[int, int]]]:
    """Insert an intro after the presentation on each successive cut batch's clock."""
    result = []
    for batch in history:
        result.append([(a + shift if a >= prefix else a, b + shift if b > prefix else b)
                       for a, b in batch])
        previous_end = removed = 0
        for start, end in sorted(batch):
            removed += max(0, min(prefix, end) - max(start, previous_end))
            previous_end = max(previous_end, end)
        if any(start < prefix < end for start, end in batch):
            shift = 0  # This cut also removes the newly inserted intro.
        prefix = max(0, prefix - removed)
    return result


def replace_intro(
    root: Path,
    video: Path,
    image: Path,
    cancel: Event | None,
    progress: Callable[[float], None] | None = None,
    *,
    encoder: str = "auto",
) -> Path:
    """Create a new MP4 with another intro, without analysis or replanning."""
    if not image.is_file():
        raise ValueError(f"Файл заставки не найден: {image}")
    state = read_variant(video)
    if not state.get("project") or not state.get("plan"):
        raise ValueError(
            "У выбранного ролика нет сохранённого плана. "
            "Быстро заменить заставку в этой старой версии нельзя."
        )
    history = cut_history(video)

    snapshot_project = Project.model_validate(state["project"])
    snapshot_project = resolve_snapshot_sources(snapshot_project, load_project(root))
    had_intro = snapshot_project.settings.intro_image is not None
    snapshot_settings = snapshot_project.settings.model_copy(update={"intro_image": image})
    snapshot_project = snapshot_project.model_copy(update={"settings": snapshot_settings})

    saved_music: Path | None = None
    music_state = state.get("music")
    if isinstance(music_state, dict) and music_state.get("path"):
        saved_music = (video.parent / str(music_state["path"])).resolve()
        if not saved_music.is_file():
            raise ValueError("Сохранённая музыка этого варианта не найдена.")

    render_settings = snapshot_settings.model_copy(
        update={
            "music_path": saved_music,
            "music_folder": None,
        }
    )
    render_project = snapshot_project.model_copy(update={"settings": render_settings})
    plan = RenderPlan.model_validate(state["plan"])
    plan_settings = dict(plan.settings)
    plan_settings.pop("request_key", None)
    plan_settings.update(
        {
            "intro_fingerprint": intro_signature(render_settings),
            "music_fingerprint": music_signature(render_settings),
        }
    )
    plan = plan.model_copy(update={"settings": plan_settings})

    destination = next_variant_path(
        video.parent, "intro-variant", snapshot_settings.preset
    )

    if history and not had_intro:
        still, overlap = intro_frames(snapshot_settings, plan.output_fps, plan.actual_frame_count)
        shift = round((still - overlap) * 1_000_000 / plan.output_fps)
        prefix = (round(presentation_frames(snapshot_settings, plan.output_fps)
                        * 1_000_000 / plan.output_fps)
                  if snapshot_settings.presentation_position == "start" else 0)
        history = shift_intro_history(history, prefix, shift)

    if history:
        from sculptcut.output_cuts import cut_output

        with tempfile.TemporaryDirectory(prefix="reintro-", dir=video.parent) as scratch:
            base = Path(scratch) / "base.mp4"
            render(
                root, render_project, plan, base, cancel, encoder=encoder,
                progress=(lambda value: progress(value * 0.5)) if progress else None,
                variant_project=snapshot_project,
            )
            result = base
            for index, cuts in enumerate(history):
                def cut_progress(value: float, batch_index: int = index) -> None:
                    if progress:
                        progress(0.5 + (batch_index + value) / (2 * len(history)))

                result = cut_output(
                    result, cuts, cancel or Event(),
                    cut_progress,
                )
            if cancel is not None and cancel.is_set():
                from sculptcut.media import Cancelled

                raise Cancelled("Обновление заставки отменено")
            os.link(result, destination)
            save_variant(
                destination, project=snapshot_project, plan=plan, music=saved_music,
                parent=video, operation="intro", history=history,
            )
        result = destination
    else:
        result = render(
            root, render_project, plan, destination, cancel, encoder=encoder,
            progress=progress, variant_project=snapshot_project,
            variant_parent=video, variant_operation="intro",
        )
    current = load_project(root)
    save_project(
        root,
        current.model_copy(
            update={"settings": current.settings.model_copy(update={"intro_image": image})}
        ),
    )
    if progress:
        progress(1)
    return result
