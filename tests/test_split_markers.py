"""Manual seams remain visible and splitting never pans or zooms the timeline."""
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from test_cursor_playback import finish
from test_episode_roll import roll_movie as roll_movie
from test_source_selection import session
from test_split_episode import open_editor

from sculptcut.source_selection import SourceSelection


def test_split_anchors_follow_history_retiming_and_legacy_exports():
    edit = session()
    assert edit.split_points() == set()  # Automatic scene boundaries stay ordinary.
    edit.split_at(400)
    edit.split_at(700)
    points = {('0', 1_200_000), ('0', 2_100_000)}
    assert edit.split_points() == points
    edit.undo()
    assert edit.split_points() == {('0', 1_200_000)}
    edit.undo(redo=True)
    assert edit.split_points() == points
    edit.fit_duration()
    assert edit.split_points() == points
    exported = edit.export_edit()
    reopened = SourceSelection(exported)
    assert reopened.split_points() == points
    # Files from stage314 have pieces but no marker metadata. Their clean
    # episode_base_plan lets us identify actual manual seams, not every cut.
    legacy = exported.plan.model_copy(update={'settings': {
        k: v for k, v in exported.plan.settings.items() if k != 'selection_split_points'}})
    reopened = SourceSelection(replace(exported, plan=legacy))
    assert reopened.split_points() == points
    reopened.select('0', 1_200_000, 2_100_000, None)
    assert reopened.plan.actual_frame_count > 0


def test_native_split_keeps_off_center_zoom_and_persists_visible_seams(roll_movie):
    app = QApplication.instance() or QApplication([])
    root, _, _, _, video = roll_movie
    window, dialog = open_editor(root, video, app)
    artifacts = Path('artifacts/hover-keys-328/split-markers')
    artifacts.mkdir(parents=True, exist_ok=True)
    try:
        track = dialog.montage_seek
        _, a, b = track.blocks[1]
        position = a+(b-a)//2
        fps = dialog.session.plan.output_fps
        seam = round(round(position*fps/1000)*1000/fps)
        dialog.seek_montage(position, follow=False)
        track.set_view(a-1000, 2200)
        view = track.view_start, track.view_span
        x = round(track.x_at(position))
        before = list(track.blocks)
        track.setFocus()
        window.grab().save(str(artifacts/'before.png'))
        QTest.keyClick(track, Qt.Key.Key_Q)
        assert (track.view_start, track.view_span) == view
        assert round(track.x_at(position)) == x
        assert track.value() == position
        assert track.selected_row == 2
        assert track.split_positions == [seam]
        assert track.blocks[1][1] == before[1][1]
        assert track.blocks[2][2] == before[1][2]
        assert track.blocks[3][1:] == before[2][1:]
        window.grab().save(str(artifacts/'after.png'))
        # The seam is visible even under the white playhead, with a dark gap.
        image = track.grab().toImage()
        seam_x = round(track.x_at(seam))
        assert image.pixelColor(seam_x-1, 39).name() == '#73d8e8'
        assert image.pixelColor(seam_x-3, 39).name() == '#111923'
        point = QPointF(seam_x, 39)
        app.sendEvent(track, QMouseEvent(QEvent.Type.MouseMove, point,
            track.mapToGlobal(point.toPoint()), Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
        assert track.toolTip() == 'Ручной разрез'
        # A second split at another off-center position keeps exactly the same view.
        second = seam+200
        second_seam = round(round(second*fps/1000)*1000/fps)
        dialog.seek_montage(second, follow=False)
        # Q follows the hovered point now, rather than a programmatic seek.
        point = QPointF(track.x_at(second), 39)
        app.sendEvent(track, QMouseEvent(QEvent.Type.MouseMove, point,
            track.mapToGlobal(point.toPoint()), Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
        QTest.keyClick(track, Qt.Key.Key_Q)
        assert (track.view_start, track.view_span) == view
        assert track.split_positions == [seam, second_seam]
        QTest.keyClick(track, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert track.split_positions == [seam]
        QTest.keyClick(track, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
        assert track.split_positions == [seam, second_seam]
        expected = dialog.session.plan
        window.save_trim_draft(force=True)
    finally:
        finish(window, dialog, app)
    window, dialog = open_editor(root, video, app)
    try:
        assert dialog.session.plan == expected
        assert dialog.montage_seek.split_positions == [seam, second_seam]
        # Removing a split piece keeps the still-existing endpoint marked and
        # maps other marks onto the newly fitted output clock.
        dialog.seek_montage(position+100, follow=False)
        track = dialog.montage_seek
        point = QPointF(track.x_at(position+100), 39)
        app.sendEvent(track, QMouseEvent(QEvent.Type.MouseMove, point,
            track.mapToGlobal(point.toPoint()), Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
        QTest.keyClick(dialog.montage_seek, Qt.Key.Key_Delete)
        assert len(dialog.montage_seek.split_positions) >= 1
        QTest.keyClick(dialog.montage_seek, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert dialog.montage_seek.split_positions == [seam, second_seam]
    finally:
        finish(window, dialog, app)
