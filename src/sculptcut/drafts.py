"""Preview revisions are editable snapshots; only explicit export uses originals."""

from collections.abc import Callable
from pathlib import Path
from threading import Event

from sculptcut.combined_edits import prepare_combined_edit, render_combined_edit
from sculptcut.output_naming import next_output_path
from sculptcut.variants import read_variant


def is_draft(video: Path) -> bool:
    return bool((read_variant(video).get("plan") or {}).get("settings", {}).get("draft"))


def ensure_draft(root: Path, video: Path, cancel: Event,
                 progress: Callable[[float], None]) -> Path:
    """Editing an older final MP4 starts a preview revision, preserving that file."""
    if is_draft(video):
        return video
    edit = prepare_combined_edit(root, video, apply_current=False, exact_variant=True)
    destination = next_output_path(root / "previews", video.stem + "-draft",
                                    edit.project.settings.preset)
    return render_combined_edit(root, edit, destination, cancel, progress, preview=True)
