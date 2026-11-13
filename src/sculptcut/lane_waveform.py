"""Bounded source-audio peaks for the music lane."""
import math
import tempfile
import zipfile
from pathlib import Path
from threading import Event

import numpy as np
from numpy.typing import NDArray

from sculptcut.media import Cancelled, discover, run_process
from sculptcut.music_library import audio_duration
from sculptcut.project import cache_key


def source_wave(root: Path, path: Path, cancel: Event) -> tuple[float, NDArray[np.float32]]:
    stat = path.stat()
    duration = audio_duration(path, cancel)
    rate = 4000
    block = max(40, math.ceil(duration*rate/120_000))
    step = block/rate
    folder = root/'cache'/'lane-wave'
    folder.mkdir(parents=True, exist_ok=True)
    key = cache_key('lane-peaks-stereo-v2', str(path.resolve()),
                    stat.st_size, stat.st_mtime_ns, block)
    cached = folder/f'{key}.npz'
    if cached.is_file():
        try:
            with zipfile.ZipFile(cached) as archive:
                if (archive.namelist() != ['peaks.npy']
                        or archive.getinfo('peaks.npy').file_size > 120_001*4+1024):
                    raise ValueError('Invalid waveform cache')
            with np.load(cached, allow_pickle=False) as data:
                values = np.asarray(data['peaks'], dtype=np.float32)
                if (values.ndim == 1 and 0 < len(values) <= 120_001
                        and np.isfinite(values).all()
                        and np.all((values >= 0) & (values <= 1))):
                    return step, values
        except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile):
            pass
    with tempfile.TemporaryDirectory(prefix='lane-wave-', dir=folder) as scratch:
        pcm = Path(scratch)/'audio.f32'
        run_process([discover('ffmpeg'), '-v', 'error', '-nostdin', '-i', str(path),
            '-map', '0:a:0', '-vn', '-ac', '2', '-ar', str(rate),
            '-t', f'{duration:.6f}', '-f', 'f32le', str(pcm)],
            cancel)
        chunks = []
        with pcm.open('rb') as stream:
            while True:
                if cancel.is_set():
                    raise Cancelled('Waveform cancelled')
                samples = np.fromfile(stream, dtype='<f4', count=block*256*2)
                if not len(samples):
                    break
                np.nan_to_num(samples, copy=False, nan=0, posinf=1, neginf=-1)
                samples = np.pad(samples, (0, (-len(samples)) % (block*2)))
                chunks.append(np.max(np.abs(samples.reshape(-1, block, 2)), axis=(1, 2)))
        values = np.concatenate(chunks).astype(np.float32) if chunks else np.zeros(1, np.float32)
        values = np.clip(values, 0, 1)
        tmp = Path(scratch)/'peaks.npz'
        np.savez_compressed(tmp, peaks=values)
        if cancel.is_set():
            raise Cancelled('Waveform cancelled')
        tmp.replace(cached)
    return step, values
