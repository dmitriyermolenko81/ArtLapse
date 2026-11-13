"""Native transport and envelope handles, with delivered audio verification."""
import json
import wave
from threading import Event

import numpy as np
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from test_cursor_playback import finish, open_editor
from test_episode_panel import wait_until
from test_music_edit import music_movie as music_movie

from sculptcut.export_storage import metadata_path
from sculptcut.media import discover, run_process
from sculptcut.music_edit import MusicClip, apply_music_edit, music_gain
from sculptcut.rendering import verify_output
from sculptcut.timeline_appearance import lane_bounds


def test_real_space_and_fade_handles_history_split_and_guards(music_movie):
    root, video, clips = music_movie
    app = QApplication.instance() or QApplication([])
    window, source = open_editor(root, video, app)
    music = source.music_track
    assert music is not None
    try:
        clip = MusicClip(path=clips[0].path, start_us=0, end_us=3_000_000, at_us=0)
        music.commit([clip])
        window.play_button.setFocus()
        QTest.keyClick(window.play_button, Qt.Key.Key_Space)
        wait_until(app, lambda: music.worker is None and source.playback.is_advancing(), 25)
        QTest.keyClick(window.play_button, Qt.Key.Key_Space)
        assert not source.sequence.desired
        music.lane.setFocus()
        QTest.keyClick(music.lane, Qt.Key.Key_Space)
        wait_until(app, lambda: source.playback.is_advancing(), 15)
        QTest.keyClick(source.montage_seek, Qt.Key.Key_Space)
        assert not source.sequence.desired
        window.output_name.setFocus()
        window.output_name.setText('track')
        QTest.keyClick(window.output_name, Qt.Key.Key_Space)
        assert window.output_name.text() == 'track ' and not source.sequence.desired
        music.lane.setFocus()
        music.lane.set_view(0, 3000)
        app.processEvents()
        top, _ = lane_bounds(music.lane)
        assert not music.lane.fade_active(music.clips[0], 'fade_in')
        assert not music.lane.fade_active(music.clips[0], 'fade_out')
        music.toggle_fade('fade_in')
        music.toggle_fade('fade_out')
        original = list(music.clips)
        for ending in ('jitter', 'escape', 'focus'):
            start = QPoint(round(music.lane.fade_x(music.clips[0], 'fade_in')), top+5)
            QTest.mousePress(music.lane, Qt.MouseButton.LeftButton, pos=start)
            if ending == 'escape':
                QTest.keyClick(music.lane, Qt.Key.Key_Escape)
            elif ending == 'focus':
                window.play_button.setFocus()
            QTest.mouseRelease(music.lane, Qt.MouseButton.LeftButton, pos=start+QPoint(1, 0))
            assert music.clips == original
        for mode, ms in (('fade_in', 600), ('fade_out', 2400)):
            old = list(music.clips)
            start = QPoint(round(music.lane.fade_x(music.clips[0], mode)), top+5)
            end = QPoint(round(music.lane.x_at(ms)), top+5)
            QTest.mousePress(music.lane, Qt.MouseButton.LeftButton, pos=start)
            assert music.lane.drag and music.lane.drag[1] == mode
            QTest.mouseMove(music.lane, end)
            QTest.mouseRelease(music.lane, Qt.MouseButton.LeftButton, pos=end)
            assert 500_000 < getattr(music.clips[0], mode+'_us') < 700_000
            changed = list(music.clips)
            music.undo()
            assert music.clips == old
            music.redo()
            assert music.clips == changed
        before = music.clips[0]
        music.lane.setValue(200)
        music.split()
        left, right = music.clips
        assert 0 < music_gain(left, left.end_us) < 1
        assert music_gain(left, left.end_us) == music_gain(right, right.start_us)
        assert music_gain(right, 2_900_000) == music_gain(before, 2_900_000)
        gain = music_gain(right, 300_000)
        music.drag_edit(1, 'fade_out', 100_000)
        assert music_gain(music.clips[1], 300_000) == gain
        assert window.grab().save(str(root/'handles.png'))
    finally:
        finish(window, source, app)


def test_real_fades_are_in_export_and_saved_metadata(music_movie):
    root, video, clips = music_movie
    clip = MusicClip(path=clips[0].path, start_us=0, end_us=3_000_000,
        at_us=0, fade_in_us=500_000, fade_out_us=500_000)
    output = apply_music_edit(root, video, [clip], Event())
    verify_output(output, 90, 30, expected_audio=True)
    wav = root/'fades.wav'
    run_process([discover('ffmpeg'), '-v', 'error', '-i', str(output), '-vn',
        '-ac', '1', '-ar', '48000', '-c:a', 'pcm_s16le', str(wav)])
    with wave.open(str(wav)) as reader:
        data = np.frombuffer(reader.readframes(reader.getnframes()), dtype='<i2').astype(float)
    def rms(a, b):
        return np.sqrt(np.mean(data[round(a*48000):round(b*48000)]**2))
    middle = rms(1, 1.2)
    assert middle > 100
    assert rms(.02, .08) < middle*.2
    assert rms(2.92, 2.98) < middle*.2
    saved = json.loads(metadata_path(output, '.music-edit.json').read_text(encoding='utf-8'))
    assert MusicClip.model_validate(saved[0]) == clip

def test_stereo_fades_preserve_both_distinct_channels(tmp_path):
    from sculptcut.music_edit import assemble_music

    track = tmp_path/'stereo.wav'
    run_process([discover('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i',
        'aevalsrc=sin(2*PI*300*t)|sin(2*PI*750*t):s=48000:d=2', str(track)])
    clip = MusicClip(path=track, start_us=0, end_us=2_000_000, at_us=0,
                     fade_in_us=400_000, fade_out_us=400_000)
    result = assemble_music(tmp_path, [clip], 2_000_000, Event())
    wav = tmp_path/'decoded.wav'
    run_process([discover('ffmpeg'), '-v', 'error', '-i', str(result),
        '-c:a', 'pcm_s16le', str(wav)])
    with wave.open(str(wav)) as reader:
        assert reader.getnchannels() == 2
        data = np.frombuffer(reader.readframes(reader.getnframes()), dtype='<i2').reshape(-1, 2)
    for channel, expected in enumerate((300, 750)):
        middle = data[48000:57600, channel].astype(float)
        hz = np.argmax(abs(np.fft.rfft(middle)))*48000/len(middle)
        assert abs(hz-expected) < 5
        for a, b in ((.01, .05), (1.95, 1.99)):
            part = data[round(a*48000):round(b*48000), channel].astype(float)
            assert np.sqrt(np.mean(part**2)) < np.sqrt(np.mean(middle**2))*.15

def test_main_window_space_without_inline_editor_keeps_typing_safe(monkeypatch):
    from sculptcut.ui import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    calls = []
    monkeypatch.setattr(window, 'play_pause', lambda: calls.append('play'))
    try:
        window.show()
        window.activateWindow()
        window.setFocus()
        app.processEvents()
        QTest.keyClick(window, Qt.Key.Key_Space)
        assert calls == ['play']
        window.output_name.setText('name')
        window.output_name.setFocus()
        QTest.keyClick(window.output_name, Qt.Key.Key_Space)
        assert window.output_name.text() == 'name '
        assert calls == ['play']
    finally:
        window.close()
        app.processEvents()
