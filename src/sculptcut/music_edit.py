"""Experimental source-time music clips, independent of video montage decisions."""
import hashlib
import json
import os
import tempfile
from pathlib import Path
from threading import Event

from pydantic import Field, model_validator

from sculptcut.domain import Model, Project
from sculptcut.export_storage import metadata_path
from sculptcut.media import Cancelled, discover, probe, run_process
from sculptcut.music_library import audio_duration, music_signature
from sculptcut.output_naming import next_variant_path
from sculptcut.planning import RenderPlan
from sculptcut.project import atomic_write
from sculptcut.shorts_layout import MUSIC_SHORTS_PRESETS
from sculptcut.variants import cut_history, read_variant, save_variant


class MusicClip(Model):
    path: Path
    start_us: int = Field(ge=0)
    end_us: int = Field(gt=0)
    at_us: int = Field(ge=0)
    volume: float = Field(default=1, ge=0, le=2)
    fade_in_us: int = Field(default=0, ge=0)
    fade_out_us: int = Field(default=0, ge=0)
    envelope_start_us: int | None = Field(default=None, ge=0)
    envelope_end_us: int | None = Field(default=None, ge=0)

    @model_validator(mode='after')
    def interval(self) -> 'MusicClip':
        if self.end_us <= self.start_us:
            raise ValueError('Конец фрагмента должен быть позже начала.')
        if self.fade_in_us or self.fade_out_us:
            if self.envelope_start_us is None:
                object.__setattr__(self, 'envelope_start_us', self.start_us)
            if self.envelope_end_us is None:
                object.__setattr__(self, 'envelope_end_us', self.end_us)
        return self


def music_gain(clip: MusicClip, source_us: int) -> float:
    start = clip.envelope_start_us if clip.envelope_start_us is not None else clip.start_us
    end = clip.envelope_end_us if clip.envelope_end_us is not None else clip.end_us
    return clip.volume*min(1., max(0., (source_us-start)/clip.fade_in_us)
        if clip.fade_in_us else 1.)*min(1., max(0., (end-source_us)/clip.fade_out_us)
        if clip.fade_out_us else 1.)


def music_volume_filter(clip: MusicClip) -> str:
    start = clip.envelope_start_us if clip.envelope_start_us is not None else clip.start_us
    end = clip.envelope_end_us if clip.envelope_end_us is not None else clip.end_us
    expression = str(clip.volume)
    if clip.fade_in_us:
        expression += f'*min(1,max(0,(t+{(clip.start_us-start)/1e6})/{clip.fade_in_us/1e6}))'
    if clip.fade_out_us:
        expression += f'*min(1,max(0,({(end-clip.start_us)/1e6}-t)/{clip.fade_out_us/1e6}))'
    if not clip.fade_in_us and not clip.fade_out_us:
        return f'volume={clip.volume}'
    return f"aeval=exprs='val(ch)*{expression}':c=same"


LOCKED_MUSIC_REASON = ('Музыка привязана к ритму. Ручные изменения недоступны. '
                       'Для другой музыки пересоберите ролик.')


def music_edit_locked(video: Path) -> bool:
    state = read_variant(video)
    preset = None
    for field in ('project', 'plan'):
        snapshot = state.get(field) or {}
        settings = snapshot.get('settings', {})
        if not isinstance(settings, dict):
            raise ValueError('Повреждённая история настроек ролика.')
        preset = settings.get('preset')
        if preset is not None and not isinstance(preset, str):
            raise ValueError('Повреждённый режим ролика.')
        if preset:
            break
    return preset in MUSIC_SHORTS_PRESETS


def insert_music_clip(clips: list[MusicClip], new: MusicClip) -> list[MusicClip]:
    """Fill only the free interval at the cursor, preserving every existing clip."""
    end = new.at_us+new.end_us-new.start_us
    for clip in clips:
        if clip.at_us <= new.at_us < clip.at_us+clip.end_us-clip.start_us:
            raise ValueError('Здесь уже есть музыка. Выберите свободный промежуток.')
        if clip.at_us > new.at_us:
            end = min(end, clip.at_us)
    added = new.model_copy(update={'end_us': new.start_us+end-new.at_us})
    return sorted([*clips, added], key=lambda c: c.at_us)


def validate_clips(clips: list[MusicClip], duration_us: int) -> None:
    end = 0
    if not clips:
        raise ValueError('Добавьте хотя бы один музыкальный фрагмент.')
    for clip in sorted(clips, key=lambda c: c.at_us):
        if clip.at_us < end:
            raise ValueError('Музыкальные фрагменты не должны перекрываться.')
        end = clip.at_us+clip.end_us-clip.start_us
        if end > duration_us:
            raise ValueError('Музыкальный фрагмент выходит за конец ролика.')


def draft_path(root: Path, video: Path) -> Path:
    key = hashlib.sha256(str(video.resolve()).casefold().encode()).hexdigest()[:20]
    return root/'music-edit-drafts'/f'{key}.json'


def save_draft(root: Path, video: Path, clips: list[MusicClip]) -> None:
    stat = video.stat()
    atomic_write(draft_path(root, video), json.dumps({'version': 1,
        'video': str(video.resolve()), 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
        'clips': [c.model_dump(mode='json') for c in clips]}, ensure_ascii=False))


def load_draft(root: Path, video: Path) -> list[MusicClip]:
    path = draft_path(root, video)
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or not all(
            key in data for key in ('version', 'video', 'size', 'mtime_ns', 'clips')):
        raise ValueError('Повреждённый музыкальный черновик.')
    stat = video.stat()
    if (data['version'] != 1 or data['video'] != str(video.resolve())
            or data['size'] != stat.st_size or data['mtime_ns'] != stat.st_mtime_ns):
        raise ValueError('Музыкальный черновик относится к другой версии видео.')
    return parse_music_clips(data['clips'])


def parse_music_clips(data: object) -> list[MusicClip]:
    """Validate persisted lanes without discarding intentional tails or silence."""
    if not isinstance(data, list):
        raise ValueError('Повреждённый список музыкальных фрагментов.')
    clips = [MusicClip.model_validate(c) for c in data]
    end = 0
    for clip in sorted(clips, key=lambda c: c.at_us):
        if clip.at_us < end:
            raise ValueError('Музыкальные фрагменты не должны перекрываться.')
        end = clip.at_us+clip.end_us-clip.start_us
    return clips


def assemble_music(root: Path, clips: list[MusicClip], duration_us: int,
                   cancel: Event) -> Path:
    if clips:
        validate_clips(clips, duration_us)
    for clip in clips:
        if clip.end_us > round(audio_duration(clip.path, cancel)*1e6)+1000:
            raise ValueError(f'Фрагмент выходит за конец трека: {clip.path.name}')
    folder = root/'music'/'manual'
    folder.mkdir(parents=True, exist_ok=True)
    # Assemble one input at a time: long music lanes never open hundreds of decoders.
    identity = [(c.model_dump(mode='json'), c.path.stat().st_size, c.path.stat().st_mtime_ns)
                for c in clips]
    key = hashlib.sha256(json.dumps(
        [4, duration_us, identity], sort_keys=True).encode()).hexdigest()
    output = folder/f'{key}.flac'
    if output.is_file():
        return output
    with tempfile.TemporaryDirectory(prefix='music-edit-', dir=folder) as scratch:
        work = Path(scratch)
        parts: list[Path] = []
        cursor = 0
        def silence(length: int) -> None:
            path = work/f'{len(parts)}.wav'
            run_process([discover('ffmpeg'), '-v', 'error', '-nostdin', '-f', 'lavfi',
                '-i', 'anullsrc=r=48000:cl=stereo', '-t', f'{length/1e6:.6f}',
                '-c:a', 'pcm_s16le', str(path)], cancel)
            parts.append(path)
        for clip in sorted(clips, key=lambda c: c.at_us):
            if clip.at_us > cursor:
                silence(clip.at_us-cursor)
            path = work/f'{len(parts)}.wav'
            run_process([discover('ffmpeg'), '-v', 'error', '-nostdin', '-ss',
                f'{clip.start_us/1e6:.6f}', '-i', str(clip.path), '-map', '0:a:0', '-vn', '-af',
                f'atrim=duration={(clip.end_us-clip.start_us)/1e6:.6f},'
                f'asetpts=PTS-STARTPTS,{music_volume_filter(clip)},aresample=48000',
                '-ac', '2', '-c:a', 'pcm_s16le', str(path)], cancel)
            parts.append(path)
            cursor = clip.at_us+clip.end_us-clip.start_us
        if cursor < duration_us:
            silence(duration_us-cursor)
        listing = work/'parts.txt'
        listing.write_text(''.join(f"file '{p.name}'\n" for p in parts), encoding='utf-8')
        result = work/'result.flac'
        run_process([discover('ffmpeg'), '-v', 'error', '-nostdin', '-f', 'concat',
            '-safe', '1', '-i', str(listing), '-af',
            f'asetpts=N/SR/TB,apad,atrim=duration={duration_us/1e6:.6f}',
            '-c:a', 'flac', str(result)], cancel)
        os.replace(result, output)
    return output


def mux_music(video: Path, music: Path, output: Path, duration_us: int, cancel: Event) -> None:
    if output.exists() or output.resolve() in (video.resolve(), music.resolve()):
        raise ValueError('Выберите новый путь результата.')
    run_process([discover('ffmpeg'), '-v', 'error', '-nostdin', '-i', str(video),
        '-i', str(music), '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy',
        '-c:a', 'aac', '-b:a', '192k', '-t', f'{duration_us/1e6:.6f}',
        '-movflags', '+faststart', str(output)], cancel)


def apply_music_edit(root: Path, video: Path, clips: list[MusicClip], cancel: Event,
                     *, destination: Path | None = None, parent: Path | None = None,
                     preview: bool = False, operation: str = 'music') -> Path:
    info = probe(video)
    if not info.duration_us:
        raise ValueError('Не удалось определить длительность видео.')
    music = assemble_music(root, clips, info.duration_us, cancel)
    state = read_variant(video)
    project = Project.model_validate(state['project']) if state.get('project') else None
    preset = project.settings.preset if project else 'timelapse'
    output = destination or next_variant_path(video.parent, 'manual-music', preset)
    if output.exists():
        raise ValueError('Output must be a new path')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='music-version-', dir=video.parent) as scratch:
        result = Path(scratch)/'result.mp4'
        mux_music(video, music, result, info.duration_us, cancel)
        run_process([discover('ffmpeg'), '-v', 'error', '-xerror', '-i', str(result),
                     '-f', 'null', '-'], cancel)
        if cancel.is_set():
            raise Cancelled('Music edit cancelled')
        os.rename(result, output)
    if project:
        project = project.model_copy(update={'settings': project.settings.model_copy(update={
            'music_path': music, 'music_folder': None, 'music_loop': False,
            'music_fade_seconds': 0})})
    plan = RenderPlan.model_validate(state['plan']) if state.get('plan') else None
    if plan:
        plan = plan.model_copy(update={'settings': {
            k: v for k, v in plan.settings.items() if k != 'request_key'} | {
                'draft': preview, 'music_fingerprint': music_signature(project.settings)
                if project else plan.settings.get('music_fingerprint', '')}})
    save_variant(output, project=project, plan=plan, music=music, parent=parent or video,
        operation=operation, history=cut_history(video),
        bookend_cuts=state.get('bookend_cuts_us'),
        episode_base_plan=RenderPlan.model_validate(state['episode_base_plan'])
            if state.get('episode_base_plan') else None,
        episode_catalogue=state.get('episode_catalogue'))
    save_draft(root, output, clips)
    # Descriptive edit data also travels with the version alongside its frozen soundtrack.
    atomic_write(metadata_path(output, '.music-edit.json'), json.dumps(
        [c.model_dump(mode='json') for c in clips], ensure_ascii=False))
    return output
