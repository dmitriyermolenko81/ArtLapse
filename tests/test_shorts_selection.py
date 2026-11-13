"""The 1.3 editor works on all moving Shorts without changing saved originals."""
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from test_cursor_playback import finish
from test_episode_panel import wait_until
from test_episode_roll import roll_movie as roll_movie
from test_split_episode import open_editor

from sculptcut.combined_edits import prepare_combined_edit
from sculptcut.domain import Roi
from sculptcut.media import probe
from sculptcut.project import save_project
from sculptcut.rendering import render, verify_output
from sculptcut.selection_history import scoped_edits
from sculptcut.shorts_layout import ACTIVE_SHORTS_PRESETS, apply_square_layout
from sculptcut.source_selection import SourceSelection
from sculptcut.variants import read_variant


@pytest.mark.parametrize('preset', ACTIVE_SHORTS_PRESETS)
@pytest.mark.parametrize('layout', ['auto', 'square'])
def test_shorts_editor_split_trim_restore_draft_export(roll_movie, preset, layout, monkeypatch):
    root, project, plan, _, original = roll_movie
    original_bytes = original.read_bytes()
    source_bytes = project.sources[0].path.read_bytes()
    project = project.model_copy(update={'settings': project.settings.model_copy(update={
        'preset': preset, 'shorts_layout': layout,
        'shorts_square_rois': {project.sources[0].id: Roi(x=.25, y=.1, width=.5, height=.8)}})})
    plan = plan.model_copy(update={'settings': plan.settings | {'preset': preset}})
    if layout == 'square':
        plan = apply_square_layout(project, plan)
    save_project(root, project)
    video = root/'renders/shorts.mp4'
    render(root, project, plan, video, encoder='libx264')
    snapshot = read_variant(video)
    project_bytes = (root/'project.json').read_bytes()
    video_bytes = video.read_bytes()
    app = QApplication.instance() or QApplication([])
    window, dialog = open_editor(root, video, app)
    try:
        assert dialog.compact
        assert dialog.session.project.settings.preset == preset
        track = dialog.montage_seek
        _, a, b = track.blocks[0]
        track.setValue((a+b)//2)
        track.setFocus()
        QTest.keyClick(track, Qt.Key.Key_Q)
        split = dialog.session.plan
        assert dialog.montage.count() == 4
        assert split.actual_frame_count == plan.actual_frame_count
        assert dialog.session.split_points()
        # Independent half removal, history, and restoration use the same editor.
        QTest.keyClick(track, Qt.Key.Key_Delete)
        assert dialog.montage.count() == 3
        assert dialog.session.plan.actual_frame_count == plan.actual_frame_count
        edits = scoped_edits(dialog.session.project, dialog.session.base,
                             dialog.session.reference, dialog.session.plan, False)
        assert edits
        dialog.session.restore_manual(edits[0])
        assert not scoped_edits(dialog.session.project, dialog.session.base,
                               dialog.session.reference, dialog.session.plan, False)
        dialog.session.undo()
        dialog.session.undo()
        assert dialog.session.plan == split
        right = split.decisions[1]
        dialog.session.resize(1, right.source_start_us+100_000,
                              right.source_end_us-100_000, 3)
        trimmed = dialog.session.plan
        assert trimmed.actual_frame_count == plan.actual_frame_count
        assert trimmed.decisions[0].crop == split.decisions[0].crop
        dialog.refresh()
        clips = []
        dialog.sequence.preparer.ready.connect(lambda clip: clips.append(clip.path))
        dialog.seek_montage(1500)
        dialog.play_montage()
        wait_until(app, lambda: bool(clips), 30)
        info = probe(clips[0])
        assert (info.width, info.height) == (360, 640)
        dialog.sequence.stop()
        window.save_trim_draft(force=True)
        artifacts = Path('artifacts/shorts-editor-318')
        artifacts.mkdir(parents=True, exist_ok=True)
        window.grab().save(str(artifacts/f'{preset}-{layout}.png'))
    finally:
        finish(window, dialog, app)
    assert (root/'project.json').read_bytes() == project_bytes
    window, dialog = open_editor(root, video, app)
    errors = []
    monkeypatch.setattr(window, 'show_error', errors.append)
    try:
        assert dialog.session.plan == trimmed
        assert dialog.session.split_points()
        split_points = dialog.session.split_points()
        dialog.apply.click()
        wait_until(app, lambda: window.job is None, 60)
        assert not errors, errors
        wait_until(app, lambda: window.selection_dialog is not None
                   and not window.selection_dialog.loading)
        dialog = window.selection_dialog
        output = dialog.video
        assert output.parent == root/'previews' and preset in output.stem
        verify_output(output, 165, 30, expected_audio=True)
        reopened = prepare_combined_edit(root, output, apply_current=False, exact_variant=True)
        assert reopened.plan.decisions == trimmed.decisions
        assert reopened.project.settings.preset == preset
        assert reopened.project.settings.shorts_layout == layout
        assert SourceSelection(reopened).split_points() == split_points
        state = read_variant(output)
        assert state['music']['tracks'] == snapshot['music']['tracks']
        assert window.export_button.isEnabled()
        window.export_button.click()
        wait_until(app, lambda: window.job is None, 60)
        assert not errors, errors
        finals = [p for p in (root/'renders').glob('*.mp4') if p not in (video, original)]
        assert len(finals) == 1
        final = finals[0]
        verify_output(final, 165, 30, expected_audio=True)
        assert read_variant(final)['plan']['decisions'] == state['plan']['decisions']
    finally:
        finish(window, dialog, app)
    assert video.read_bytes() == video_bytes
    assert original.read_bytes() == original_bytes
    assert project.sources[0].path.read_bytes() == source_bytes
