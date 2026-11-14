"""Real inline editing, independent histories, shared audition and delivered sound."""
import wave
from pathlib import Path
from threading import Event

import numpy as np
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from test_cursor_playback import finish, open_editor
from test_episode_panel import wait_until
from test_episode_roll import roll_movie as roll_movie
from test_music_edit import music_movie as music_movie

from sculptcut.media import discover, probe, run_process
from sculptcut.music_edit import MusicClip, apply_music_edit, load_draft
from sculptcut.rendering import verify_output
from sculptcut.track_edit import audible_clips, render_tracks, saved_music_clips
from sculptcut.variants import read_variant


def test_inline_lanes_focus_history_shared_clock_and_audition(music_movie):
    root, video, clips = music_movie
    app = QApplication.instance() or QApplication([])
    window, source = open_editor(root, video, app)
    music = source.music_track
    assert music is not None
    original = source.session.plan
    try:
        window.resize(1440, 860)
        wait_until(app, lambda: music.lane.mapTo(window, QPoint()).y()
                   > source.montage_seek.mapTo(window, QPoint()).y())
        assert music.isVisible() and not music.isWindow()
        assert source.montage_seek.height()+music.height() <= 112
        assert music.lane.height() == 40 and not music.lane.show_ruler
        assert music.lane.left_padding == source.montage_seek.left_padding == 30
        assert music.lane.lane_icon == 'music'
        assert source.montage_seek.lane_icon == 'video'
        assert music.lane.accessibleName() == music.lane.lane_label
        assert source.montage_seek.lane_label in source.montage_seek.toolTip()
        assert window.video.height() >= 300
        assert music.lane.mapTo(window, QPoint()).y() > source.montage_seek.mapTo(
            window, QPoint()).y()
        assert not music.picture.isVisible() and not music.save_button.isVisible()
        music.commit(clips)
        assert not music.options_button.isVisible()
        QTest.mouseClick(music.lane, Qt.MouseButton.RightButton,
            pos=QPoint(round(music.lane.x_at(1000)), 20))
        assert music.options_menu.isVisible()
        assert any(action.text() == 'Добавить трек' for action in music.options_menu.actions())
        music.options_menu.hide()
        assert source.session.plan == original and not source.session.changed
        assert source.apply.isEnabled()
        assert not window.export_button.isEnabled()
        music.lane.setFocus()
        music.lane.navigate(1000)
        assert source.montage_seek.value() == music.lane.value() == 1000
        QTest.keyClick(music.lane, Qt.Key.Key_Q)
        assert len(music.clips) == 3 and source.session.plan == original
        QTest.keyClick(music.lane, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert music.clips == clips and source.session.plan == original
        QTest.keyClick(music.lane, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
        assert len(music.clips) == 3
        music.undo()
        source.montage_seek.setFocus()
        source.seek_montage(1000)
        QTest.keyClick(source.montage_seek, Qt.Key.Key_Q)
        assert source.session.changed and music.clips == clips
        QTest.keyClick(source.montage_seek, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert not source.session.changed and music.clips == clips
        source.montage_seek.set_view(500, 1500)
        assert (music.lane.view_start, music.lane.view_span) == (500, 1500)
        music.lane.set_view(0, 3000)
        assert source.montage_seek.view_start == 0 and source.montage_seek.view_span == 2999
        # Tiny pointer jitter, focus loss and Esc never commit a music edit.
        music.lane.setFocus()
        x = round(music.lane.x_at(900))
        for ending in ('jitter', 'focus', 'escape'):
            QTest.mousePress(music.lane, Qt.MouseButton.LeftButton, pos=QPoint(x, 20))
            if ending == 'focus':
                source.montage_seek.setFocus()
            elif ending == 'escape':
                QTest.keyClick(music.lane, Qt.Key.Key_Escape)
            QTest.mouseRelease(music.lane, Qt.MouseButton.LeftButton, pos=QPoint(x+2, 20))
            assert music.clips == clips
        source.seek_montage(600)
        source.play_montage()
        wait_until(app, lambda: music.worker is None and source.sequence.enabled
                   and source.playback.is_advancing(), 25)
        wait_until(app, lambda: music.player.position() >= 600)
        assert source.playback.audio.isMuted() and not music.audio.isMuted()
        assert music.player.source().toLocalFile().endswith('.flac')
        assert abs(music.player.position()-source.montage_seek.value()) < 250
        source.play_montage()
        assert not source.sequence.desired
        assert music.player.playbackState().name != 'PlayingState'
        folder = Path('artifacts/compact-tracks-327')
        folder.mkdir(exist_ok=True, parents=True)
        assert window.grab().save(str(folder/'inline.png'))
    finally:
        finish(window, source, app)


def test_bookend_cut_and_final_export_keep_absolute_music_gap(roll_movie):
    from sculptcut.combined_edits import prepare_combined_edit

    root, project, _, _, video = roll_movie
    app = QApplication.instance() or QApplication([])
    window, source = open_editor(root, video, app)
    try:
        assert project.settings.music_path is not None
        clips = [MusicClip(path=project.settings.music_path, start_us=0,
                           end_us=1_000_000, at_us=700_000)]
        source.music_track.commit(clips)
        source.trim_movie(500, source.montage_seek.maximum())
        edit = source.session.export_edit()
        assert edit.bookend_cuts()
        preview = root/'previews/bookend-tracks.mp4'
        render_tracks(root, edit, preview, Event(), lambda _: None,
            clips=clips, video_changed=True, encoder='libx264')
        final_edit = prepare_combined_edit(root, preview, apply_current=False, exact_variant=True)
        final = root/'renders/bookend-tracks.mp4'
        render_tracks(root, final_edit, final, Event(), lambda _: None,
            clips=saved_music_clips(preview), video_changed=True, preview=False, encoder='libx264')
        for i, output in enumerate((preview, final)):
            decoded = root/f'bookend-{i}.wav'
            run_process([discover('ffmpeg'), '-v', 'error', '-i', str(output), '-vn',
                '-ac', '1', '-ar', '48000', '-c:a', 'pcm_s16le', str(decoded)])
            with wave.open(str(decoded)) as reader:
                samples = np.frombuffer(reader.readframes(reader.getnframes()), dtype='<i2')
            assert np.abs(samples[1000:round(.6*48000)].astype(float)).max() < 15
            assert np.std(samples[round(.8*48000):round(1.5*48000)]) > 500
            assert load_draft(root, output) == clips
            info = probe(output)
            verify_output(output, round(info.duration_us*30/1e6), 30, expected_audio=True)
    finally:
        finish(window, source, app)
    assert load_draft(root, video) == clips


@pytest.mark.parametrize('video_changed', [False, True])
def test_real_apply_tracks_preserves_music_clock_and_originals(music_movie, video_changed):
    root, video, clips = music_movie
    app = QApplication.instance() or QApplication([])
    window, source = open_editor(root, video, app)
    music = source.music_track
    assert music is not None
    untouched = {p: p.read_bytes() for p in (video, root/'project.json', clips[0].path)}
    try:
        music.commit(clips)
        if video_changed:
            row, a, b = source.montage_seek.blocks[0]
            source.commit_edge(row, 'end', (a+b)//2)
            assert source.session.changed and music.clips == clips
        edit = source.session.export_edit()
        output = root/'previews/tracks.mp4'
        render_tracks(root, edit, output, Event(), lambda _: None,
                      clips=clips, video_changed=video_changed, encoder='libx264')
        info = probe(output)
        verify_output(output, round(info.duration_us*30/1e6), 30, expected_audio=True)
        assert {p: p.read_bytes() for p in untouched} == untouched
        assert load_draft(root, output) == clips
        state = read_variant(output)
        assert state['parent'] == video.name
        assert state['plan']['decisions'] == edit.plan.model_dump(mode='json')['decisions']
        assert audible_clips(clips, 1_000_000)[0].at_us == 500_000
        assert audible_clips(clips, 1_000_000)[0].end_us == 1_500_000
        assert clips[0].end_us == 2_000_000
        decoded = root/'check.wav'
        run_process([discover('ffmpeg'), '-v', 'error', '-i', str(output), '-vn',
            '-ac', '1', '-ar', '48000', '-c:a', 'pcm_s16le', str(decoded)])
        with wave.open(str(decoded)) as reader:
            samples = np.frombuffer(reader.readframes(reader.getnframes()), dtype='<i2')
        assert np.abs(samples[1000:18000].astype(float)).max() < 15
        part = samples[round(.65*48000):round(min(1.3, info.duration_us/1e6-.05)*48000)]
        assert len(part) > 1000
        frequency = np.argmax(np.abs(np.fft.rfft(part)))*48000/len(part)
        assert abs(frequency-300) < 3
        if not video_changed:
            def digest(path):
                return run_process([discover('ffmpeg'), '-v', 'error', '-i', str(path),
                    '-map', '0:v:0', '-c', 'copy', '-f', 'hash', '-hash', 'sha256', '-'])
            assert digest(output) == digest(video)
        # Reopening the new version treats the saved track state as the baseline.
        source.reject()
        app.processEvents()
        window.open_video(output, 'tracks')
        window.show_source_selection()
        wait_until(app, lambda: window.selection_dialog is not None
                   and not window.selection_dialog.loading)
        source = window.selection_dialog
        assert source.music_track is not None and source.music_track.clips == clips
        assert not source.music_track.changed
    finally:
        finish(window, source, app)


def test_delete_all_music_apply_in_main_window_and_reopen_silent(music_movie):
    root, original, clips = music_movie
    video = apply_music_edit(root, original, clips, Event())
    app = QApplication.instance() or QApplication([])
    window, source = open_editor(root, video, app)
    try:
        music = source.music_track
        assert music is not None and not music.changed
        music.select(0)
        music.lane.setFocus()
        QTest.keyClick(music.lane, Qt.Key.Key_Delete)
        QTest.keyClick(music.lane, Qt.Key.Key_Delete)
        assert music.clips == [] and music.changed and source.apply.isEnabled()
        assert not source.session.changed
        source.apply.click()
        wait_until(app, lambda: window.job is None and window.selection_dialog is not None
            and window.selection_dialog is not source and not window.selection_dialog.loading, 30)
        source = window.selection_dialog
        assert source.music_track is not None and source.music_track.clips == []
        assert not source.music_track.changed
        output = source.video
        assert read_variant(output)['plan']['settings']['draft']
        assert window.export_button.isEnabled()
        decoded = root/'silent.wav'
        run_process([discover('ffmpeg'), '-v', 'error', '-i', str(output), '-vn',
            '-ac', '1', '-c:a', 'pcm_s16le', str(decoded)])
        with wave.open(str(decoded)) as reader:
            samples = np.frombuffer(reader.readframes(reader.getnframes()), dtype='<i2')
        assert np.abs(samples.astype(float)).max() < 2
        verify_output(output, 90, 30, expected_audio=True)
        window.export_button.click()
        wait_until(app, lambda: window.job is None and window.selection_dialog is not None
            and window.selection_dialog is not source and not window.selection_dialog.loading, 30)
        source = window.selection_dialog
        assert not read_variant(source.video)['plan']['settings'].get('draft')
        verify_output(source.video, 90, 30, expected_audio=True)
        final_wave = root/'final-silent.wav'
        run_process([discover('ffmpeg'), '-v', 'error', '-i', str(source.video), '-vn',
            '-ac', '1', '-c:a', 'pcm_s16le', str(final_wave)])
        with wave.open(str(final_wave)) as reader:
            final_samples = np.frombuffer(reader.readframes(reader.getnframes()), dtype='<i2')
        assert np.abs(final_samples.astype(float)).max() < 2
    finally:
        finish(window, source, app)
