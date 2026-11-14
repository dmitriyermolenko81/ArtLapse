"""Inline music-style review protects edits while keeping frame navigation cheap."""
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QFocusEvent, QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from test_cursor_playback import finish
from test_episode_panel import wait_until
from test_episode_roll import roll_movie as roll_movie

from sculptcut.review_timeline import ReviewTimeline
from sculptcut.timeline_appearance import LanePiece
from sculptcut.ui import MainWindow


@pytest.mark.parametrize('compact', [False, True])
def test_review_click_jitter_double_click_focus_loss_and_repeat_are_safe(compact):
    app = QApplication.instance() or QApplication([])
    track = ReviewTimeline()
    track.resize(900, 104)
    if compact:
        track.compact_lane = True
        track.left_padding = 56
        track.marker_y = 46
        track.setFixedHeight(52)
    track.setRange(0, 6000)
    track.blocks = [(0, 0, 2000), (1, 2000, 6000)]
    track.pieces = [LanePiece(0, 0, 2000, '1 · sculpture', '00:00 – 00:06'),
                    LanePiece(1, 2000, 6000, '2 · sculpture', '00:06 – 00:12')]
    edits, previews, keys = [], [], []
    track.edge_commit.connect(lambda *args: edits.append(args))
    track.movie_commit.connect(lambda *args: edits.append(args))
    track.edge_preview.connect(lambda *args: previews.append(args))
    for signal in (track.split_scene, track.remove_scene,
                   track.undo_requested, track.redo_requested):
        signal.connect(lambda: keys.append(True))
    track.show()
    point = QPoint(round(track.x_at(2000)), 38 if compact else 50)
    try:
        QTest.mouseClick(track, Qt.MouseButton.LeftButton, pos=point)
        QTest.mousePress(track, Qt.MouseButton.LeftButton, pos=point)
        QTest.mouseMove(track, point+QPoint(2, 0))
        QTest.mouseRelease(track, Qt.MouseButton.LeftButton, pos=point+QPoint(2, 0))
        QTest.mouseDClick(track, Qt.MouseButton.LeftButton, pos=point)
        assert not edits
        for cancel in ('escape', 'focus', 'hide'):
            QTest.mousePress(track, Qt.MouseButton.LeftButton, pos=point)
            QTest.mouseMove(track, point+QPoint(-45, 0))
            assert previews and not edits
            if cancel == 'escape':
                QTest.keyClick(track, Qt.Key.Key_Escape)
            elif cancel == 'focus':
                app.sendEvent(track, QFocusEvent(QEvent.Type.FocusOut))
            else:
                track.hide()
                track.show()
            QTest.mouseRelease(track, Qt.MouseButton.LeftButton, pos=point+QPoint(-45, 0))
            assert not edits and not track.pointer_down
        for key, mods in ((Qt.Key.Key_Q, Qt.KeyboardModifier.NoModifier),
                          (Qt.Key.Key_Delete, Qt.KeyboardModifier.NoModifier),
                          (Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier),
                          (Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)):
            app.sendEvent(track, QKeyEvent(QEvent.Type.KeyPress, key, mods, '', True))
        QTest.keyClick(track, Qt.Key.Key_Backspace, Qt.KeyboardModifier.ControlModifier)
        assert not keys
        QTest.mousePress(track, Qt.MouseButton.LeftButton, pos=point)
        QTest.mouseMove(track, point+QPoint(-45, 0))
        assert not edits
        QTest.mouseRelease(track, Qt.MouseButton.LeftButton, pos=point+QPoint(-45, 0))
        assert len(edits) == 1 and edits[0][0:2] == (track.selected_row, 'end')
    finally:
        track.close()
        app.processEvents()


def test_main_inline_review_reuses_scene_state_and_preserves_typed_edits(roll_movie, monkeypatch):
    root, _, _, _, video = roll_movie
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    dialog = None
    window.show()
    window.folder.setText(str(root))
    window.open_video(video, 'test')
    try:
        wait_until(app, lambda: window.selection_dialog is not None
                   and not window.selection_dialog.loading)
        dialog = window.selection_dialog
        wait_until(app, lambda: not dialog.frame_image.isNull())
        track = dialog.montage_seek
        assert isinstance(track, ReviewTimeline) and not dialog.isWindow()
        assert track.window() is window and track.height() == 52
        original = dialog.session.plan
        _, start, end = track.blocks[0]
        dialog.seek_montage(start+100)
        choose = dialog.choose_scene
        calls = []
        def counted(*args, **kwargs):
            calls.append(True)
            return choose(*args, **kwargs)
        monkeypatch.setattr(dialog, 'choose_scene', counted)
        for i in range(10):
            dialog.seek_montage(min(end-1, start+100+i*10), follow=False)
        assert not calls and dialog.saved_scrub
        assert dialog.scrub_frames.wanted[0] == video.resolve()
        dialog.more.click()
        assert dialog.fragment_menu.isVisible()
        dialog.fragment_menu.actions()[0].trigger()
        dialog.fragment_menu.hide()
        assert dialog.details.isVisible()
        dialog.start.setFocus()
        QTest.keyClick(dialog.start, Qt.Key.Key_Return)
        QTest.keyClick(dialog.start, Qt.Key.Key_Q)
        global_undos = []
        window.edit_history.record('Guard input', 0, 1, global_undos.append)
        history_position = window.edit_history.position
        QTest.keyClick(dialog.start, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(dialog.start, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
        assert window.edit_history.position == history_position and not global_undos
        assert window.job is None and dialog.session.plan == original
        dialog.details.hide()
        track.setFocus()
        track.navigate((start+end)//2)
        dialog.flush_navigation()
        QTest.keyClick(track, Qt.Key.Key_Q)
        assert dialog.session.plan != original
        QTest.keyClick(track, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert dialog.session.plan == original
        window.resize(1120, 760)
        app.processEvents()
        folder = Path('artifacts/compact-tracks-327')
        folder.mkdir(exist_ok=True, parents=True)
        assert window.grab().save(str(folder/'main-inline.png'))
        assert track.grab().save(str(folder/'review-lane.png'))
    finally:
        if dialog is not None:
            finish(window, dialog, app)
        else:
            window._allow_close = True
            window.close()


def test_no_top_grips_and_ctrl_wheel_still_zooms_both_lanes(roll_movie, tmp_path):
    from test_cursor_playback import open_editor
    from test_timeline_navigation import wheel

    root, _, _, _, video = roll_movie
    app = QApplication.instance() or QApplication([])
    window, editor = open_editor(root, video, app)
    try:
        track = editor.montage_seek
        music = editor.music_track
        assert music is not None and track.height() == 52
        original = editor.session.plan
        history = list(editor.session.undo_stack)
        clips = list(music.clips)
        commits = []
        track.movie_commit.connect(lambda *args: commits.append(args))
        for ms, shift in ((0, 40), (track.maximum(), -40)):
            point = QPoint(round(track.x_at(ms)), 3)
            QTest.mousePress(track, Qt.MouseButton.LeftButton, pos=point)
            QTest.mouseMove(track, point+QPoint(shift, 0))
            QTest.mouseRelease(track, Qt.MouseButton.LeftButton, pos=point+QPoint(shift, 0))
        assert not commits
        span = track.view_span
        wheel(app, track, Qt.KeyboardModifier.ControlModifier)
        assert 0 < track.view_span < span
        assert (music.lane.view_start, music.lane.view_span) == (
            track.view_start, track.view_span)
        assert editor.session.plan == original
        assert editor.session.undo_stack == history
        assert music.clips == clips
        assert not hasattr(track, 'zoom_handles')
        wait_until(app, lambda: track.isVisible() and not editor.frame_image.isNull())
        app.processEvents()
        assert window.grab().save(str(tmp_path/'no-grips.png'))
    finally:
        finish(window, editor, app)


def test_inline_messages_only_use_main_bottom_status(roll_movie, tmp_path):
    from test_cursor_playback import open_editor

    root, _, _, _, video = roll_movie
    app = QApplication.instance() or QApplication([])
    window, editor = open_editor(root, video, app)
    try:
        wait_until(app, lambda: not editor.frame_image.isNull())
        app.processEvents()
        assert editor.layout_box.indexOf(editor.message) == -1
        before = editor.height()
        for message in ('Music changed; rebuild the plan', 'Ошибка проверки'):
            editor.fail(message)
            app.processEvents()
            assert window.status.text() == message
            assert editor.message.isHidden()
            assert editor.height() == before
        editor.set_message('Ошибка загрузки')
        editor.scrub_frames.quick_busy.emit(True)
        assert window.status.text() == 'Ошибка загрузки'
        assert editor.message.isHidden()
        assert window.grab().save(str(tmp_path/'bottom-status.png'))
    finally:
        finish(window, editor, app)


@pytest.mark.parametrize('action', ['KEEP', 'SPEED', 'HOLD', 'MONTAGE', 'ramp'])
def test_video_labels_describe_actual_edit(action):
    from sculptcut.domain import SpeedRamp
    from sculptcut.planning import RenderDecision
    from sculptcut.review_timeline import video_piece

    decision = RenderDecision(source_id='source', source_start_us=10_000_000,
        source_end_us=30_000_000, output_frame_count=60,
        action='SPEED' if action == 'ramp' else action, provenance='test',
        sample_times_us=[10_000_000, 20_000_000] if action == 'MONTAGE' else [],
        speed_ramp=SpeedRamp(start_us=10_000_000, end_us=30_000_000, strength=.4)
            if action == 'ramp' else None)
    piece = video_piece(24, 1000, decision, 30, '2025-07-08 22-27-21.mp4')
    assert piece.tiny_title == '25'
    assert '2025-07-08' not in piece.title
    assert '2025-07-08 22-27-21.mp4' in piece.tooltip
    assert '00:01.0–00:03.0' in piece.tooltip and '2.0' in piece.tooltip
    if action == 'HOLD':
        assert 'Стоп' in piece.title and '×' not in piece.title
        assert '00:30' not in piece.title
    elif action == 'MONTAGE':
        assert 'Кадры' in piece.title and 'Серия кадров: 2' in piece.tooltip
        assert '×' not in piece.title
    else:
        assert '×10' in piece.short_title
        assert ('≈' in piece.short_title) == (action == 'ramp')
        assert '00:10.0–00:30.0' in piece.tooltip
    assert ':' not in piece.title and len(piece.title) < 20


def test_video_label_details_are_available_on_hover(roll_movie, tmp_path):
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QMouseEvent
    from test_cursor_playback import open_editor

    root, _, _, _, video = roll_movie
    app = QApplication.instance() or QApplication([])
    window, editor = open_editor(root, video, app)
    try:
        wait_until(app, lambda: not editor.frame_image.isNull())
        track = editor.montage_seek
        piece = track.pieces[0]
        original = editor.session.plan
        assert piece.tiny_title == '1'
        assert piece.tooltip and 'В исходнике:' in piece.tooltip
        app.processEvents()
        point = QPoint(round(track.x_at((piece.start_ms+piece.end_ms)/2)), 25)
        app.sendEvent(track, QMouseEvent(QEvent.Type.MouseMove, QPointF(point),
            QPointF(track.mapToGlobal(point)), Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
        assert track.toolTip() == piece.tooltip
        assert editor.session.plan == original
        assert window.grab().save(str(tmp_path/'episode-labels.png'))
    finally:
        finish(window, editor, app)


def test_bottom_statistics_keep_analysis_count_when_editing_saved_version(roll_movie, tmp_path):
    import json

    from test_cursor_playback import open_editor

    from sculptcut.export_storage import metadata_path

    root, _, _, _, video = roll_movie
    path = metadata_path(video, '.variant.json')
    state = json.loads(path.read_text(encoding='utf-8'))
    state['plan']['settings']['work_verified_count'] = 84
    path.write_text(json.dumps(state), encoding='utf-8')
    app = QApplication.instance() or QApplication([])
    window, editor = open_editor(root, video, app)
    try:
        wait_until(app, lambda: not editor.frame_image.isNull())
        count = editor.montage.count()
        assert 'После анализа: 84' in window.statistics.text()
        assert f'В ролике: {count}' in window.statistics.text()
        _, a, b = editor.montage_seek.blocks[0]
        editor.seek_montage((a+b)//2)
        editor.split_scene()
        assert editor.montage.count() == count+1
        assert 'После анализа: 84' in window.statistics.text()
        assert f'В ролике: {count+1}' in window.statistics.text()
        assert window.grab().save(str(tmp_path/'analysis-count.png'))
    finally:
        finish(window, editor, app)
