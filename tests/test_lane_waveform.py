"""Real audio peaks, cache reuse and asynchronous music-lane display."""
from threading import Event

import numpy as np
from PySide6.QtWidgets import QApplication
from test_cursor_playback import finish, open_editor
from test_episode_panel import wait_until
from test_music_edit import music_movie as music_movie

from sculptcut.lane_waveform import source_wave
from sculptcut.media import discover, run_process
from sculptcut.music_edit import MusicClip


def test_real_stereo_peaks_silence_and_cached_reuse(tmp_path, monkeypatch):
    path = tmp_path/'opposite.wav'
    run_process([discover('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i',
        'aevalsrc=if(lt(t\\,1)\\,0.8*sin(2*PI*300*t)\\,0)|'
        'if(lt(t\\,1)\\,-0.8*sin(2*PI*300*t)\\,0):s=8000:d=2', str(path)])
    step, levels = source_wave(tmp_path, path, Event())
    assert np.median(levels[:round(.8/step)]) > .5
    assert np.max(levels[round(1.2/step):]) < .01
    cache = next((tmp_path/'cache'/'lane-wave').glob('*.npz'))
    np.savez_compressed(cache, peaks=np.array([-1], dtype=np.float32))
    _, repaired = source_wave(tmp_path, path, Event())
    assert np.array_equal(repaired, levels)
    monkeypatch.setattr('sculptcut.lane_waveform.run_process',
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('decoded twice')))
    again_step, again = source_wave(tmp_path, path, Event())
    assert again_step == step and np.array_equal(again, levels)


def test_wave_is_loaded_in_background_and_follows_clip(music_movie):
    root, video, clips = music_movie
    app = QApplication.instance() or QApplication([])
    window, source = open_editor(root, video, app)
    music = source.music_track
    assert music is not None
    try:
        track = root/'wave-signal.wav'
        run_process([discover('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i',
            'aevalsrc=if(lt(t\\,1)\\,0.8*sin(2*PI*300*t)\\,0)|'
            'if(lt(t\\,1)\\,-0.8*sin(2*PI*300*t)\\,0):s=8000:d=2', str(track)])
        movie = root/'wave-signal.mp4'
        run_process([discover('ffmpeg'), '-v', 'error', '-i', str(video), '-i', str(track),
            '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'aac', str(movie)])
        clip = MusicClip(path=movie, start_us=0, end_us=2_000_000, at_us=300_000)
        music.commit([clip])
        assert music.lane.waveforms.get(clip.path) is None
        wait_until(app, lambda: clip.path in music.lane.waveforms, 20)
        step, peaks = music.lane.waveforms[clip.path]
        assert step > 0 and np.max(peaks) > .05
        image = music.lane.grab().toImage()
        assert image.save(str(root/'lane.png'))
        wave = music.lane.waveforms.pop(clip.path)
        blank = music.lane.grab().toImage()
        center = music.lane.height()//2
        def differences(start, end):
            return sum(image.pixelColor(x, center-6) != blank.pixelColor(x, center-6)
                       for x in range(round(music.lane.x_at(start)),
                                      round(music.lane.x_at(end))))
        assert differences(450, 1150) > 20
        assert differences(1450, 2150) == 0
        music.lane.waveforms[clip.path] = wave
        old = list(music.clips)
        music.lane.setValue(1300)
        music.split()
        assert len(music.clips) == 2
        assert music.lane.waveforms[clip.path][1] is peaks
        assert old[0].path in music.lane.waveforms
        music.commit([clip.model_copy(update={'start_us': 1_200_000, 'at_us': 0})])
        trimmed = music.lane.grab().toImage()
        saved_wave = music.lane.waveforms.pop(clip.path)
        without_wave = music.lane.grab().toImage()
        assert all(trimmed.pixelColor(x, center-6) == without_wave.pixelColor(x, center-6)
                   for x in range(round(music.lane.x_at(100)), round(music.lane.x_at(700))))
        music.lane.waveforms[clip.path] = saved_wave
    finally:
        finish(window, source, app)
