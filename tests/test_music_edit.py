"""Manual music edits preserve video packets and produce the requested audio intervals."""
import json
import wave
from pathlib import Path
from threading import Event

import numpy as np
import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QWheelEvent
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from test_cursor_playback import finish
from test_episode_panel import wait_until

from sculptcut.domain import ProjectSettings
from sculptcut.fixtures import generate_fixture
from sculptcut.media import Cancelled, discover, run_process
from sculptcut.music_edit import (
    MusicClip,
    apply_music_edit,
    assemble_music,
    load_draft,
    save_draft,
    validate_clips,
)
from sculptcut.music_edit_dialog import MusicEditDialog, MusicLane
from sculptcut.planning import build_plan
from sculptcut.project import create_project, save_project
from sculptcut.rendering import render, verify_output
from sculptcut.simple_timeline import SimpleTimeline
from sculptcut.ui import MainWindow
from sculptcut.variants import read_variant


@pytest.fixture
def music_movie(tmp_path):
    source = generate_fixture(tmp_path/'source.mp4', 'local', duration=4, fps=10)
    root = tmp_path/'project'
    project = create_project(root, [source]).model_copy(update={'settings': ProjectSettings(
        target_seconds=3, final_view_seconds=0, output_name='Manual music')})
    save_project(root, project)
    plan = build_plan(project, [], 3)
    video = root/'previews/original.mp4'
    render(root, project, plan, video, preview=True, encoder='libx264')
    tracks = []
    for frequency in (300, 750):
        track = tmp_path/f'{frequency}.wav'
        run_process([discover('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i',
                     f'sine=frequency={frequency}:duration=4', str(track)])
        tracks.append(track)
    clips = [MusicClip(path=tracks[0], start_us=1_000_000, end_us=2_000_000, at_us=500_000),
             MusicClip(path=tracks[1], start_us=0, end_us=1_000_000, at_us=2_000_000)]
    return root, video, clips


def test_real_music_intervals_silence_packet_copy_history_and_reopen(music_movie):
    root, video, clips = music_movie
    before = {p: p.read_bytes() for p in (video, root/'project.json', clips[0].path, clips[1].path)}
    music = assemble_music(root, clips, 3_000_000, Event())
    assert assemble_music(root, clips, 3_000_000, Event()) == music
    wav = root/'decoded.wav'
    run_process([discover('ffmpeg'), '-v', 'error', '-i', str(music), '-ac', '1',
                 '-ar', '48000', '-c:a', 'pcm_s16le', str(wav)])
    with wave.open(str(wav)) as reader:
        samples = np.frombuffer(reader.readframes(reader.getnframes()), dtype='<i2').astype(float)
    assert len(samples) == 144_000
    assert np.abs(samples[:20000]).max() == 0
    assert np.abs(samples[75000:94000]).max() == 0
    for a, b, frequency in ((.6, 1.4, 300), (2.1, 2.9, 750)):
        part = samples[round(a*48000):round(b*48000)]
        found = np.argmax(np.abs(np.fft.rfft(part)))*48000/len(part)
        assert abs(found-frequency)<2
    output = apply_music_edit(root, video, clips, Event())
    verify_output(output, 90, 30, expected_audio=True)
    def video_hash(path):
        return run_process([discover('ffmpeg'), '-v', 'error', '-i', str(path),
            '-map', '0:v:0', '-c', 'copy', '-f', 'hash', '-hash', 'sha256', '-'])
    assert video_hash(output) == video_hash(video)
    assert read_variant(output)['plan']['decisions'] == read_variant(video)['plan']['decisions']
    assert not read_variant(output)['plan']['settings']['draft']
    assert load_draft(root, output) == clips
    assert {p: p.read_bytes() for p in before} == before


def test_invalid_overlap_bounds_cancel_and_draft(music_movie):
    root, video, clips = music_movie
    save_draft(root, video, clips)
    assert load_draft(root, video) == clips
    for invalid in ([], [clips[0].model_copy(update={'at_us': 2_500_000})],
                    [clips[0], clips[1].model_copy(update={'at_us': 1_000_000})]):
        with pytest.raises(ValueError):
            validate_clips(invalid, 3_000_000)
    with pytest.raises(ValueError):
        assemble_music(root, [MusicClip(path=clips[0].path, start_us=5_000_000,
            end_us=6_000_000, at_us=0)], 3_000_000, Event())
    cancelled = Event()
    cancelled.set()
    with pytest.raises(Cancelled):
        assemble_music(root, clips, 3_000_000, cancelled)


def test_native_music_dialog_drag_trim_undo_preview_save_and_reopen(music_movie):
    root, video, clips = music_movie
    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    dialog = MusicEditDialog(root, video, owner)
    dialog.show()
    saved = []
    dialog.version_saved.connect(saved.append)
    try:
        dialog.commit(clips)
        dialog.select(0)
        lane = dialog.lane
        start = round(.9/3*lane.width())
        QTest.mousePress(lane, Qt.MouseButton.LeftButton, pos=QPoint(start, 40))
        QTest.mouseRelease(lane, Qt.MouseButton.LeftButton, pos=QPoint(start+20, 40))
        assert dialog.clips[0].at_us > clips[0].at_us
        dialog.undo()
        assert dialog.clips == clips
        dialog.select(0)
        assert not dialog.details.isVisible()
        assert dialog.save_button.isEnabled()
        dialog.options_button.click()
        assert dialog.options_menu.isVisible()
        assert dialog.options_menu.actions()[0].text() == "Добавить трек"
        dialog.options_menu.hide()
        QTest.mouseDClick(lane, Qt.MouseButton.LeftButton, pos=QPoint(start, 40))
        assert dialog.details.isVisible()
        dialog.start.setValue(1.2)
        # Enter applies exact values without an extra Apply button or file picker.
        QTest.keyClick(dialog.start, Qt.Key.Key_Return)
        assert dialog.clips[0].start_us == 1_200_000
        assert not dialog.volume_state.silent
        dialog.hide_details_button.click()
        assert not dialog.details.isVisible()
        edited = dialog.clips
        dialog.listen_button.click()
        wait_until(app, lambda: dialog.worker is None, 20)
        assert dialog.player.source().toLocalFile().endswith('preview-1.mp4')
        assert not dialog.audio.isMuted()
        wait_until(app, lambda: not dialog.frame_image.isNull())
        dialog.player.pause()
        dialog.lane.seek.emit(800_000)
        dialog.toggle_play()
        wait_until(app, lambda: dialog.player.position() >= 800)
        dialog.apply()
        wait_until(app, lambda: dialog.worker is None, 20)
        assert len(saved) == 1 and Path(saved[0]).is_file()
        artifacts = Path('artifacts/music-editor-323')
        artifacts.mkdir(exist_ok=True, parents=True)
        assert dialog.grab().save(str(artifacts/'music-editor.png'))
        (artifacts/'gui.json').write_text(json.dumps({'status':'PASS','clips':len(edited)}))
    finally:
        dialog.close()
        wait_until(app, lambda: dialog.worker is None)
        app.processEvents()
    reopened = MusicEditDialog(root, Path(saved[0]), owner)
    assert reopened.clips == edited
    reopened.show()
    reopened.preview()
    reopened.close()
    wait_until(app, lambda: reopened.worker is None and not reopened.isVisible())
    app.processEvents()


def test_music_lane_empty_add_trim_limits_and_keyboard_delete(music_movie, monkeypatch):
    root, video, clips = music_movie
    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    dialog = MusicEditDialog(root, video, owner)
    dialog.show()
    from PySide6.QtWidgets import QFileDialog
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *a: (str(clips[0].path), ''))
    try:
        app.processEvents()
        assert not dialog.listen_button.isEnabled() and not dialog.save_button.isEnabled()
        assert not dialog.details.isVisible()
        wait_until(app, lambda: not dialog.frame_image.isNull())
        artifacts = Path('artifacts/music-editor-323')
        artifacts.mkdir(exist_ok=True, parents=True)
        assert dialog.grab().save(str(artifacts/'music-empty.png'))
        QTest.mouseClick(dialog.lane, Qt.MouseButton.LeftButton, pos=QPoint(60, 55))
        assert len(dialog.clips) == 1 and dialog.clips[0].end_us == 3_000_000
        dialog.drag_edit(0, 'right', -2_000_000)
        assert dialog.clips[0].end_us == 1_000_000
        dialog.add_button.click()
        assert len(dialog.clips) == 2 and dialog.clips[1].at_us == 1_000_000
        dialog.drag_edit(0, 'move', 9_000_000)
        assert dialog.clips[0].at_us == 0  # stops at neighbour
        dialog.select(1)
        dialog.lane.setFocus()
        QTest.keyClick(dialog.lane, Qt.Key.Key_Delete)
        assert len(dialog.clips) == 1
        dialog.undo_button.click()
        assert len(dialog.clips) == 2
        dialog.drag_edit(1, 'left', -9_000_000)
        assert dialog.clips[1].at_us == 1_000_000  # source start and neighbour bound
        dialog.select(0)
        dialog.show_details()
        dialog.end.setValue(9)
        QTest.keyClick(dialog.end, Qt.Key.Key_Return)
        assert dialog.clips[0].end_us == 1_000_000  # invalid typed bound is rejected
    finally:
        dialog.close()
        app.processEvents()


def test_main_window_opens_experimental_lane_from_music_button(music_movie, monkeypatch):
    root, video, clips = music_movie
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    window.open_project_folder(root)
    wait_until(app, lambda: window.selection_dialog is not None
               and not window.selection_dialog.loading)
    selection = window.selection_dialog
    try:
        assert window.manual_music_button.isEnabled()
        errors = []
        monkeypatch.setattr(window, 'show_error', errors.append)
        selection.loading = True
        window.manual_music_button.click()
        assert errors and selection.music_track is not None
        assert not selection.music_track.isWindow()
        selection.loading = False
        window.manual_music_button.click()
        dialog = window.findChild(MusicEditDialog)
        assert dialog is not None and dialog.isVisible()
        assert dialog is selection.music_track and dialog.lane.hasFocus()
        assert dialog.volume_state is window.volume_state
        assert dialog.audio.volume() == pytest.approx(window.volume_state.level/100)
        assert dialog.video == video
        dialog.commit(clips)
        selection.hide()
        app.processEvents()
        assert load_draft(root, video) == clips
    finally:
        finish(window, selection, app)


def test_music_keyboard_split_history_preview_and_real_save(music_movie):
    root, video, clips = music_movie
    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    dialog = MusicEditDialog(root, video, owner)
    dialog.show()
    track = dialog.lane
    try:
        dialog.commit(clips)
        wait_until(app, lambda: not dialog.frame_image.isNull())
        app.processEvents()
        track.setFocus()
        QTest.keyClick(track, Qt.Key.Key_Home)
        QTest.keyClick(track, Qt.Key.Key_Right)
        assert track.value() == 33
        assert dialog.time_label.text().startswith('00:00.03 / ')
        QTest.keyClick(track, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
        assert track.value() == 367
        QTest.keyClick(track, Qt.Key.Key_End)
        assert track.value() == 3000
        QTest.keyClick(track, Qt.Key.Key_Up)
        assert dialog.row == 0 and track.value() == 500
        QTest.keyClick(track, Qt.Key.Key_Down)
        assert dialog.row == 1 and track.value() == 2000
        track.navigate(1001)
        QTest.keyClick(track, Qt.Key.Key_Q)
        split = dialog.clips
        assert len(split) == 3 and dialog.row == 1
        assert split[0].end_us == split[1].start_us == 1_500_000
        assert split[1].at_us == 1_000_000 and split[2] == clips[1]
        assert sum(c.end_us-c.start_us for c in split) == 2_000_000
        QTest.keyClick(track, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert dialog.clips == clips
        QTest.keyClick(track, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
        assert dialog.clips == split
        QTest.keyClick(track, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(track, Qt.Key.Key_Z,
                       Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        assert dialog.clips == split
        dialog.select(1)
        QTest.keyClick(track, Qt.Key.Key_Backspace)
        assert dialog.clips == [split[0], split[2]]
        QTest.keyClick(track, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert dialog.clips == split
        QTest.keyClick(track, Qt.Key.Key_Delete)
        assert dialog.clips != split
        dialog.undo()
        dialog.drag_edit(0, 'right', -100_000)
        edited = dialog.clips
        assert not dialog.redo_stack
        QTest.keyClick(track, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
        assert dialog.clips == edited
        dialog.undo()
        assert dialog.clips == split
        track.navigate(750)
        app.sendEvent(track, QKeyEvent(QEvent.Type.KeyPress, ord('Й'),
            Qt.KeyboardModifier.NoModifier, 'й'))
        assert len(dialog.clips) == 4
        dialog.undo()
        for ms in (0, 500, 1000, 1500, 1750, 2000, 3000):
            track.navigate(ms)
            QTest.keyClick(track, Qt.Key.Key_Q)
            assert dialog.clips == split
        track.navigate(750)
        app.sendEvent(track, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Q,
            Qt.KeyboardModifier.NoModifier, 'q', True))
        assert dialog.clips == split
        dialog.select(0)
        dialog.show_details()
        cursor, history = track.value(), len(dialog.undo_stack)
        for key in (Qt.Key.Key_Q, Qt.Key.Key_F, Qt.Key.Key_Space):
            QTest.keyClick(dialog.start, key)
        QTest.keyClick(dialog.start, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert track.value() == cursor and len(dialog.undo_stack) == history
        assert dialog.clips == split and dialog.worker is None
        dialog.details.hide()
        track.setFocus()
        track.navigate(1000)
        QTest.keyClick(track, Qt.Key.Key_Space)
        wait_until(app, lambda: dialog.worker is None and dialog.audition_ready, 20)
        wait_until(app, lambda: dialog.player.playbackState() ==
                   QMediaPlayer.PlaybackState.PlayingState)
        QTest.keyClick(track, Qt.Key.Key_Space)
        assert dialog.player.playbackState() == QMediaPlayer.PlaybackState.PausedState
        # Space on the Save button must pause/resume, never create an extra version.
        dialog.save_button.setFocus()
        QTest.keyClick(dialog.save_button, Qt.Key.Key_Space)
        assert not dialog.saved_path and dialog.worker is None
        assert dialog.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        QTest.keyClick(dialog.save_button, Qt.Key.Key_Space)
        dialog.apply()
        wait_until(app, lambda: dialog.worker is None, 20)
        output = Path(dialog.saved_path)
        assert output.is_file() and load_draft(root, output) == split
        verify_output(output, 90, 30, expected_audio=True)
        folder = Path('artifacts/music-editor-323')
        folder.mkdir(exist_ok=True, parents=True)
        assert dialog.grab().save(str(folder/'keyboard-editor.png'))
    finally:
        dialog.close()
        wait_until(app, lambda: dialog.worker is None)
        app.processEvents()


def test_music_uses_episode_timeline_zoom_pan_and_navigation():
    app = QApplication.instance() or QApplication([])
    music = MusicLane(60_000_000)
    reference = SimpleTimeline()
    reference.setRange(0, 60000)
    music.clips = [MusicClip(path=Path('a.wav'), start_us=0,
        end_us=10_000_000, at_us=20_000_000)]
    music.blocks = reference.blocks = [(0, 20000, 30000)]
    changes = []
    music.edited.connect(lambda *args: changes.append(args))
    try:
        for track in (music, reference):
            track.resize(620, 104)
            track.show()
            track.set_view(20000, 10000)
            point = QPointF(210, 40)
            app.sendEvent(track, QWheelEvent(point, point, QPoint(), QPoint(0, 120),
                Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier,
                Qt.ScrollPhase.ScrollUpdate, False))
            QTest.mousePress(track, Qt.MouseButton.MiddleButton, pos=QPoint(310, 40))
            QTest.mouseMove(track, QPoint(190, 40))
            QTest.mouseRelease(track, Qt.MouseButton.MiddleButton, pos=QPoint(190, 40))
        assert music.view_span == reference.view_span == 7000
        assert music.view_start == reference.view_start
        assert not changes
        view = music.view_start
        QTest.mousePress(music, Qt.MouseButton.MiddleButton, pos=QPoint(310, 40))
        QTest.mouseMove(music, QPoint(190, 40))
        QTest.keyClick(music, Qt.Key.Key_Escape)
        QTest.mouseRelease(music, Qt.MouseButton.MiddleButton, pos=QPoint(190, 40))
        assert music.view_start == view and music.isVisible()
        for key in (Qt.Key.Key_F, Qt.Key.Key_Backslash):
            music.set_view(22000, 10000)
            QTest.keyClick(music, key)
            assert music.view_span == 60000 and music.view_start == 0
        music.selected_row = 0
        start = QPoint(round(music.x_at(25000)), 40)
        QTest.mousePress(music, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(music, start+QPoint(20, 0))
        QTest.keyClick(music, Qt.Key.Key_Escape)
        QTest.mouseRelease(music, Qt.MouseButton.LeftButton, pos=start+QPoint(20, 0))
        assert not music.drag and not music.pointer_down and not changes
    finally:
        music.close()
        reference.close()
