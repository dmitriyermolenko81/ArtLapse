"""Exercise the default full soundtrack, rather than only pre-edited music clips."""
import wave
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog
from test_cursor_playback import finish, open_editor
from test_episode_panel import wait_until
from test_hover_timeline_keys import hover
from test_music_edit import music_movie as music_movie

from sculptcut.media import discover, run_process
from sculptcut.music_edit import MusicClip, load_draft, save_draft
from sculptcut.music_edit_dialog import MusicEditDialog
from sculptcut.planning import build_plan
from sculptcut.project import load_project, save_project
from sculptcut.rendering import render, verify_output
from sculptcut.track_edit import saved_music_clips


@pytest.fixture
def full_soundtrack(music_movie):
    root, _, clips = music_movie
    project = load_project(root)
    project = project.model_copy(update={'settings': project.settings.model_copy(update={
        'music_path': clips[0].path, 'music_fade_seconds': 0})})
    save_project(root, project)
    video = root/'previews/full-soundtrack.mp4'
    render(root, project, build_plan(project, [], 3), video, preview=True, encoder='libx264')
    return root, video, clips[1].path


def test_full_soundtrack_hover_cuts_while_playing_and_inserts_from_menu(
    full_soundtrack, monkeypatch,
):
    root, video, new_track = full_soundtrack
    app = QApplication.instance() or QApplication([])
    window, source = open_editor(root, video, app)
    music = source.music_track
    assert music is not None
    try:
        wait_until(app, lambda: music.lane.mapTo(window, QPoint()).y()
                   > source.montage_seek.mapTo(window, QPoint()).y())
        assert len(music.clips) == 1 and music.clips[0].path == video
        source.play_montage()
        wait_until(app, source.playback.is_advancing, 20)
        window.play_button.setFocus()
        hover(app, music.lane, 1000)
        app.sendEvent(window.play_button, QKeyEvent(QEvent.Type.KeyPress,
            Qt.Key.Key_Q, Qt.KeyboardModifier.NoModifier))
        assert len(music.clips) == 2
        assert music.clips[0].end_us == music.clips[1].start_us
        seam = music.lane.split_positions[0]
        image = music.lane.grab().toImage()
        assert image.pixelColor(round(music.lane.x_at(seam))-1, 20).name() == '#73d8e8'
        assert source.session.changed is False
        hover(app, music.lane, 1600)
        app.sendEvent(window.play_button, QKeyEvent(QEvent.Type.KeyPress,
            Qt.Key.Key_Q, Qt.KeyboardModifier.NoModifier))
        assert len(music.clips) == 3
        point = QPoint(round(music.lane.x_at(1100)), 20)
        QTest.mouseClick(music.lane, Qt.MouseButton.RightButton, pos=point)
        assert music.row == 1 and music.options_menu.isVisible()
        music.options_menu.hide()
        before = list(music.clips)
        music.commit(before[:2]+[before[2].model_copy(update={
            'start_us': 2_500_000, 'at_us': 2_500_000})])
        retained = music.clips[-1]
        source.seek_montage(1700)
        calls = []
        def choose(*args):
            calls.append(args[0])
            return str(new_track), ''
        monkeypatch.setattr(QFileDialog, 'getOpenFileName', choose)
        menu = music.options_menu
        action = next(a for a in menu.actions() if a.text() == 'Добавить трек')
        assert not music.options_button.isVisible()
        QTest.mouseClick(music.lane, Qt.MouseButton.RightButton,
            pos=QPoint(round(music.lane.x_at(1700)), 20))
        assert menu.isVisible()
        insertion_at = source.montage_seek.value()*1000
        QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(action).center())
        assert calls
        assert any(c.path == new_track and c.at_us == insertion_at for c in music.clips)
        assert source.session.changed is False
        assert retained in music.clips
        assert source.apply.isEnabled()
        assert calls[0] is window
        assert window.status.text().startswith('Трек добавлен в свободный промежуток')
        assert not source.more.isVisible() and not window.manual_music_button.isVisible()
        assert source.montage_seek.height()+music.height() == 102
        inserted = list(music.clips)
        music.undo()
        assert all(c.path == video for c in music.clips)
        music.redo()
        assert music.clips == inserted
        folder = Path('artifacts/music-gaps-331')
        folder.mkdir(exist_ok=True, parents=True)
        assert window.grab().save(str(folder/'music.png'))
        source.apply.click()
        wait_until(app, lambda: window.job is None and window.selection_dialog is not None
            and window.selection_dialog is not source and not window.selection_dialog.loading, 30)
        source = window.selection_dialog
        output = source.video
        verify_output(output, 90, 30, expected_audio=True)
        decoded = root/'inserted.wav'
        run_process([discover('ffmpeg'), '-v', 'error', '-i', str(output), '-vn',
            '-ac', '1', '-ar', '48000', '-c:a', 'pcm_s16le', str(decoded)])
        with wave.open(str(decoded)) as reader:
            samples = np.frombuffer(reader.readframes(reader.getnframes()), dtype='<i2')
        part = samples[round(1.8*48000):round(2.4*48000)]
        frequency = np.argmax(np.abs(np.fft.rfft(part)))*48000/len(part)
        assert abs(frequency-750) < 3
        tail = samples[round(2.6*48000):round(2.9*48000)]
        tail_frequency = np.argmax(np.abs(np.fft.rfft(tail)))*48000/len(tail)
        assert abs(tail_frequency-300) < 5
        assert load_draft(root, output) == inserted
    finally:
        finish(window, source, app)


def test_music_cuts_after_video_shortening_keep_recoverable_tail(full_soundtrack):
    root, video, _ = full_soundtrack
    app = QApplication.instance() or QApplication([])
    window, source = open_editor(root, video, app)
    music = source.music_track
    assert music is not None
    try:
        original_end = music.clips[0].end_us
        row, a, b = source.montage_seek.blocks[0]
        source.commit_edge(row, 'end', (a+b)//2)
        assert music.duration < original_end
        window.play_button.setFocus()
        hover(app, music.lane, 500)
        app.sendEvent(window.play_button, QKeyEvent(QEvent.Type.KeyPress,
            Qt.Key.Key_Q, Qt.KeyboardModifier.NoModifier))
        assert len(music.clips) == 2
        assert music.clips[-1].end_us == original_end
    finally:
        finish(window, source, app)


@pytest.mark.parametrize('preset', ['music_shorts', 'photoshop_music_shorts'])
def test_beat_music_is_read_only_even_with_stale_draft_and_other_form_preset(
    full_soundtrack, monkeypatch, preset,
):
    root, _, new_track = full_soundtrack
    project = load_project(root)
    project = project.model_copy(update={'settings': project.settings.model_copy(
        update={'preset': preset})})
    save_project(root, project)
    video = root/f'previews/{preset}.mp4'
    render(root, project, build_plan(project, [], 3), video, preview=True, encoder='libx264')
    stale = [MusicClip(path=new_track, start_us=0, end_us=1_000_000, at_us=0)]
    save_draft(root, video, stale)
    app = QApplication.instance() or QApplication([])
    window, source = open_editor(root, video, app)
    music = source.music_track
    assert music is not None
    try:
        wait_until(app, lambda: music.lane.mapTo(window, QPoint()).y()
                   > source.montage_seek.mapTo(window, QPoint()).y())
        assert music.locked and music.lane.read_only
        assert music.clips[0].path == video and not music.changed
        assert not music.options_button.isEnabled()
        assert not window.manual_music_button.isEnabled()
        frozen = saved_music_clips(video)
        assert frozen and len(frozen) == 1 and frozen[0].path == video
        clips, plan = list(music.clips), source.session.plan
        monkeypatch.setattr(QFileDialog, 'getOpenFileName',
            lambda *args: pytest.fail('Locked music opened a file picker'))
        window.play_button.setFocus()
        hover(app, music.lane, 1000)
        for key, mods in ((Qt.Key.Key_Q, Qt.KeyboardModifier.NoModifier),
                          (Qt.Key.Key_Delete, Qt.KeyboardModifier.NoModifier),
                          (Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)):
            QTest.keyClick(window.play_button, key, mods)
        music.commit(stale)
        music.add()
        music.split()
        music.remove()
        music.drag_edit(0, 'right', -500_000)
        music.drag_edit(0, 'fade_in', 500_000)
        music.drag_edit(0, 'fade_out', -500_000)
        music.apply()
        assert music.clips == clips and source.session.plan == plan
        assert load_draft(root, video) == stale  # Retained, never silently applied or erased.
        source.montage_seek.setFocus()
        hover(app, source.montage_seek, 1000)
        QTest.keyClick(source.montage_seek, Qt.Key.Key_Q)
        assert source.session.changed and music.clips == clips
    finally:
        finish(window, source, app)


@pytest.mark.parametrize('preset', ['beat_shorts', 'photoshop_beat_shorts'])
def test_saved_legacy_beat_music_dialog_also_refuses_edits(full_soundtrack, preset):
    from PySide6.QtWidgets import QWidget

    root, _, new_track = full_soundtrack
    project = load_project(root)
    project = project.model_copy(update={'settings': project.settings.model_copy(
        update={'preset': preset})})
    video = root/f'previews/{preset}.mp4'
    render(root, project, build_plan(project, [], 3), video, preview=True, encoder='libx264')
    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    music = MusicEditDialog(root, video, owner)
    try:
        music.show()
        before = list(music.clips)
        music.commit([MusicClip(path=new_track, start_us=0, end_us=500_000, at_us=0)])
        music.split()
        music.remove()
        assert music.locked and music.clips == before and not music.save_button.isEnabled()
    finally:
        music.close()
        app.processEvents()
