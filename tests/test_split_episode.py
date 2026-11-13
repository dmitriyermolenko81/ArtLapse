"""Frame-accurate split, independent trimming, persistence and native Q workflow."""
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from test_cursor_playback import finish
from test_episode_panel import wait_until
from test_episode_roll import roll_movie as roll_movie
from test_source_selection import session

from sculptcut.combined_edits import prepare_combined_edit, render_combined_edit
from sculptcut.domain import SpeedRamp
from sculptcut.montage_playback import rendered_segments, reused_scene
from sculptcut.planning import validate_plan
from sculptcut.rendering import verify_output
from sculptcut.selection_duration import fit_selection, timeline_start
from sculptcut.selection_history import scoped_edits
from sculptcut.source_selection import SourceSelection
from sculptcut.timewarp import source_time
from sculptcut.ui import MainWindow


def open_editor(root, video, app):
    window = MainWindow()
    window.show()
    window.open_project_folder(root)
    wait_until(app, lambda: window.selection_dialog is not None
               and not window.selection_dialog.loading)
    dialog = window.selection_dialog
    assert dialog.video == video and dialog.isVisible()
    wait_until(app, lambda: not window.video_frame_image.isNull())
    return window, dialog


@pytest.mark.parametrize('ramp', [False, True])
def test_split_preserves_all_frames_neighbours_ramp_and_undo(ramp):
    edit = session()
    old = edit.plan.decisions[0]
    if ramp:
        old = old.model_copy(update={'speed_ramp': SpeedRamp(
            start_us=old.source_start_us, end_us=old.source_end_us, strength=.6)})
        edit.plan = edit.base = edit.plan.model_copy(update={
            'decisions': [old, *edit.plan.decisions[1:]]})
    before = edit.plan
    assert edit.split_at(400) == ('0', source_time(old, .4))
    left, right = edit.plan.decisions[:2]
    assert left.output_frame_count == 12 and right.output_frame_count == 18
    assert left.source_start_us == old.source_start_us
    assert left.source_end_us == right.source_start_us
    assert right.source_end_us == old.source_end_us
    assert left.speed_ramp == right.speed_ramp == old.speed_ramp
    assert left.crop == right.crop == old.crop
    assert edit.plan.decisions[2:] == before.decisions[1:]
    assert edit.plan.actual_frame_count == before.actual_frame_count
    assert not edit.project.overrides
    assert not scoped_edits(edit.project, edit.base, edit.reference, edit.plan, False)
    validate_plan(edit.project, edit.plan)
    for piece in (left, right):
        for fraction in (0, .25, .5, 1):
            # The curve phase stays continuous through the seam (microsecond rounding).
            local = piece.output_frame_count / old.output_frame_count
            offset = 0 if piece is left else .4
            assert abs(source_time(piece, fraction)-source_time(old, offset+fraction*local)) <= 2
    after = edit.plan
    edit.undo()
    assert edit.plan == before and not edit.changed
    edit.undo(redo=True)
    assert edit.plan == after


def test_split_can_trim_or_remove_only_one_half_and_keep_duration():
    edit = session()
    edit.project = edit.project.model_copy(update={'settings': edit.project.settings.model_copy(
        update={'target_seconds': 4.5})})
    edit.fixed_duration = True
    edit.plan = fit_selection(edit.project, edit.plan, [], [])
    original = edit.plan
    edit.split_at(400)
    split = edit.plan
    assert [d.output_frame_count for d in split.decisions[:2]] == [12, 18]
    right = split.decisions[1]
    edit.resize(1, right.source_start_us+100_000, right.source_end_us-100_000, 3)
    assert edit.plan.decisions[0].source_end_us == split.decisions[0].source_end_us
    assert edit.plan.actual_frame_count == original.actual_frame_count
    edit.undo()
    assert edit.plan == split
    edit.select('0', right.source_start_us, right.source_end_us, None)
    assert edit.plan.decisions[0].source_end_us == right.source_start_us
    assert edit.plan.decisions[0].output_frame_count > 0
    assert edit.plan.decisions[1].output_frame_count == 0
    assert edit.plan.actual_frame_count == original.actual_frame_count
    edit.undo()
    assert edit.plan == split


def test_split_at_native_speed_tolerates_only_microsecond_frame_rounding():
    edit = session()
    first = edit.plan.decisions[0].model_copy(update={'output_frame_count': 90})
    edit.plan = edit.plan.model_copy(update={'decisions': [first, *edit.plan.decisions[1:]],
        'actual_frame_count': 195, 'target_frame_count': 195})
    edit.project = edit.project.model_copy(update={'settings': edit.project.settings.model_copy(
        update={'target_seconds': 6.5})})
    edit.fixed_duration = True
    for ms in (33, 67, 100, 2967):
        assert edit.split_at(ms) is not None
    validate_plan(edit.project, edit.plan)
    assert edit.plan.actual_frame_count == 195
    assert len([d for d in edit.plan.decisions if d.output_frame_count]) == 7
    # The tolerance must not permit actual slowdown.
    bad = first.model_copy(update={'source_end_us': first.source_end_us-1000})
    pieces = [bad, edit.base.decisions[1].model_copy(update={'source_start_us': bad.source_end_us}),
              *edit.base.decisions[2:]]
    with pytest.raises(ValueError, match='below 1x'):
        validate_plan(edit.project, edit.plan.model_copy(update={'decisions': pieces}))


def test_split_ignores_seams_bookends_one_frame_and_final_still():
    edit = session()
    before = edit.plan
    for ms in (-100, 0, 1000, 2500, 4500, 5000):
        assert edit.split_at(ms) is None
    assert edit.plan == before and not edit.undo_stack
    last = edit.plan.decisions[6].model_copy(update={'action': 'HOLD',
        'provenance': 'user:final_tail'})
    edit.plan = before.model_copy(update={'decisions': [*before.decisions[:6], last,
                                                       *before.decisions[7:]]})
    assert edit.split_at(3000) is None
    edit.plan = before.model_copy(update={'decisions': [before.decisions[0].model_copy(
        update={'output_frame_count': 1}), *before.decisions[1:]],
        'actual_frame_count': 106, 'target_frame_count': 106})
    assert edit.split_at(16) is None


def test_native_q_delete_history_autosave_reopen_and_short_export(roll_movie):
    app = QApplication.instance() or QApplication([])
    root, _, _, _, video = roll_movie
    original = video.read_bytes()
    window, dialog = open_editor(root, video, app)
    output = root/'previews/split.mp4'
    try:
        track = dialog.montage_seek
        old = dialog.session.plan
        _, a, b = track.blocks[0]
        position = a + (b-a)//2
        track.setFocus()
        # Pending click navigation must resolve before Q (no stale selected episode).
        track.setValue(position)
        dialog.request_montage_seek(position)
        QTest.keyClick(track, Qt.Key.Key_Q)
        assert dialog.montage.count() == 4
        assert track.selected_row == 1
        assert abs(track.value()-position) <= 1
        assert dialog.session.plan.actual_frame_count == old.actual_frame_count
        split = dialog.session.plan
        assert len(dialog.session.undo_stack) == 1
        segments = rendered_segments(dialog.session.original.project, dialog.session.original.plan)
        for row, decision in enumerate(split.decisions[:2]):
            reused = reused_scene(decision, video, dialog.session.original.plan, segments, row)
            assert reused is not None and reused.path == video and reused.decision is None
        # Keyboard input in filename must not split the track.
        window.output_name.setFocus()
        QTest.keyClick(window.output_name, Qt.Key.Key_Q)
        assert dialog.session.plan == split
        track.setFocus()
        QTest.keyClick(track, Qt.Key.Key_Delete)
        assert dialog.montage.count() == 3
        assert dialog.session.plan.decisions[1].output_frame_count == 0
        assert dialog.session.plan.actual_frame_count == split.actual_frame_count
        QTest.keyClick(track, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert dialog.session.plan == split
        QTest.keyClick(track, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert dialog.session.plan == old
        QTest.keyClick(track, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
        assert dialog.session.plan == split
        assert track.selected_row == 1  # A shared edge belongs to the right half.
        window.save_trim_draft(force=True)
        artifacts = Path('artifacts/split-episode-314')
        artifacts.mkdir(parents=True, exist_ok=True)
        window.grab().save(str(artifacts/'split-ui.png'))
    finally:
        finish(window, dialog, app)
    window, dialog = open_editor(root, video, app)
    try:
        assert dialog.session.plan == split
        assert dialog.montage.count() == 4
        export = dialog.session.export_edit()
        render_combined_edit(root, export, output, Event(), lambda _: None,
            encoder='libx264', preview=True, operation='source_selection')
        verify_output(output, 165, 30, expected_audio=True)
        reopened = prepare_combined_edit(root, output, [], apply_current=False, exact_variant=True)
        assert len([d for d in reopened.plan.decisions if d.output_frame_count]) == 4
        assert reopened.plan.decisions[:2] == split.decisions[:2]
        assert SourceSelection(reopened).split_points() == dialog.session.split_points()
        assert video.read_bytes() == original
        wait_until(app, lambda: dialog.preparer.worker is None)
    finally:
        finish(window, dialog, app)


def test_presentation_offset_and_russian_q(roll_movie):
    app = QApplication.instance() or QApplication([])
    root, _, _, _, video = roll_movie
    window, dialog = open_editor(root, video, app)
    try:
        edit = dialog.session
        offset = timeline_start(edit.project, edit.plan, edit.original.leading)
        assert offset > 0
        assert edit.split_at(round(offset*500/edit.plan.output_fps)) is None
        row, a, b = dialog.montage_seek.blocks[0]
        dialog.seek_montage(a+(b-a)//2)
        app.sendEvent(dialog.montage_seek, QKeyEvent(
            QEvent.Type.KeyPress, 0x419, Qt.KeyboardModifier.NoModifier, 'й'))
        assert dialog.montage.count() == 4
        assert dialog.montage_seek.selected_row == row+1
        assert edit.project.overrides == edit.original.project.overrides
        assert not SourceSelection(replace(edit.export_edit(), plan=edit.plan)).changed
    finally:
        finish(window, dialog, app)
