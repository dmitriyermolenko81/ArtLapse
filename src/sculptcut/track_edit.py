"""Independent soundtrack intervals on the delivered video's absolute clock."""
import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from threading import Event

from sculptcut.combined_edits import CombinedEdit
from sculptcut.export_storage import metadata_path
from sculptcut.media import probe
from sculptcut.music_edit import (
    MusicClip,
    apply_music_edit,
    music_edit_locked,
    parse_music_clips,
)


def audible_clips(clips: list[MusicClip], duration_us: int) -> list[MusicClip]:
    """Keep draft tails intact; only the delivered sound stops at the video's end."""
    return [c.model_copy(update={'end_us': min(c.end_us, c.start_us+duration_us-c.at_us)})
            for c in clips if c.at_us < duration_us]


def saved_music_clips(video: Path) -> list[MusicClip] | None:
    if music_edit_locked(video):
        info = probe(video)
        if not info.duration_us:
            raise ValueError('Не удалось определить длительность видео.')
        return [MusicClip(path=video, start_us=0, end_us=info.duration_us, at_us=0)] if (
            info.audio_streams) else []
    path = metadata_path(video, '.music-edit.json')
    return (parse_music_clips(json.loads(path.read_text(encoding='utf-8')))
            if path.is_file() else None)


def render_tracks(root: Path, edit: CombinedEdit, destination: Path, cancel: Event,
                  progress: Callable[[float], None], *, clips: list[MusicClip] | None,
                  video_changed: bool, encoder: str = 'auto',
                  encoder_status: Callable[[str], None] | None = None,
                  preview: bool = True) -> Path:
    from sculptcut.combined_edits import render_combined_edit

    if clips is None:
        return render_combined_edit(root, edit, destination, cancel, progress,
            encoder=encoder, encoder_status=encoder_status, preview=preview,
            operation='source_selection')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='tracks-', dir=destination.parent) as scratch:
        video = edit.parent
        if video_changed:
            video = render_combined_edit(root, edit, Path(scratch)/'video.mp4', cancel,
                lambda value: progress(value*.85), encoder=encoder,
                encoder_status=encoder_status, preview=preview, operation='source_selection')
        duration = probe(video).duration_us
        if not duration:
            raise ValueError('Не удалось определить длительность видео.')
        result = apply_music_edit(root, video, audible_clips(clips, duration), cancel,
            destination=destination, parent=edit.parent, preview=preview,
            operation=('source_selection' if preview else 'combined_edit')
                if video_changed else 'music')
        # Preserve the complete absolute-time music draft if the video was shortened.
        from sculptcut.music_edit import save_draft

        save_draft(root, result, clips)
        from sculptcut.project import atomic_write

        atomic_write(metadata_path(result, '.music-edit.json'), json.dumps(
            [c.model_dump(mode='json') for c in clips], ensure_ascii=False))
        progress(1)
        return result
