"""Deliver a reviewed original-quality MP4 with its self-contained edit history."""

import json
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from threading import Event

from sculptcut.domain import Project
from sculptcut.export_storage import metadata_path, publish_export
from sculptcut.media import Cancelled
from sculptcut.music_edit import draft_path, parse_music_clips, save_draft
from sculptcut.output_cuts import saved_soundtrack
from sculptcut.planning import RenderPlan
from sculptcut.project import (
    atomic_write,
    fingerprint,
    load_project,
    resolve_snapshot_sources,
    validate_sources,
)
from sculptcut.variants import cut_history, read_variant, save_variant


def copy_review(root: Path, video: Path, destination: Path, cancel: Event,
                progress: Callable[[float], None], *, draft: bool) -> bool:
    state = read_variant(video)
    if not state.get("plan") or not state.get("project"):
        return False
    signature = fingerprint(video)
    if state.get("video_fingerprint") and signature != state["video_fingerprint"]:
        return False
    plan = RenderPlan.model_validate(state["plan"])
    if plan.settings.get("draft") and plan.settings.get("preview_quality") != "original":
        return False
    if not plan.settings.get("draft") and video.parent != root / "renders":
        return False
    if not state.get("video_fingerprint"):
        from sculptcut.media import MediaError
        from sculptcut.remux_check import video_samples

        try:
            if video_samples(video, cancel) <= 0:
                return False
        except Cancelled:
            raise
        except MediaError:
            return False
    project = resolve_snapshot_sources(Project.model_validate(state["project"]), load_project(root))
    validate_sources(root, project)
    plan = plan.model_copy(update={"settings": plan.settings | {
        "draft": int(draft), "preview_quality": "original"}})
    music = saved_soundtrack(video)
    destination.parent.mkdir(parents=True, exist_ok=True)
    lane = metadata_path(video, ".music-edit.json")
    guard = metadata_path(video, ".guard.json")
    if not guard.is_file():
        guard = video.with_suffix(".guard.json")
    target_lane = metadata_path(destination, ".music-edit.json")
    target_guard = metadata_path(destination, ".guard.json")
    with tempfile.TemporaryDirectory(dir=destination.parent) as scratch:
        ready = Path(scratch) / "review.mp4"
        total = video.stat().st_size
        with video.open("rb") as source, ready.open("xb") as output:
            while block := source.read(4 * 1024**2):
                if cancel.is_set():
                    raise Cancelled("Сохранение отменено")
                output.write(block)
                progress(0.9 * source.tell() / total)
        if fingerprint(video) != signature:
            raise ValueError("Файл изменился во время экспорта.")
        with publish_export(ready, destination, cancel, extra_files=(
                target_guard, target_lane, draft_path(root, destination))):
            save_variant(destination, ready=ready, project=project, plan=plan,
                         music=music[0] if music else None,
                         parent=video, operation=state.get("operation", "render"),
                         cuts=state.get("cuts_us"), history=cut_history(video),
                         bookend_cuts=state.get("bookend_cuts_us"),
                         episode_base_plan=RenderPlan.model_validate(state["episode_base_plan"])
                         if state.get("episode_base_plan") else None)
            if lane.is_file():
                data = lane.read_text(encoding="utf-8")
                save_draft(root, destination, parse_music_clips(json.loads(data)), ready=ready)
                atomic_write(target_lane, data)
            if guard.is_file():
                shutil.copy2(guard, target_guard)
    progress(1)
    return True
