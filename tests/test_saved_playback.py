"""Reopened trimmed exports play through cuts as one saved A/V stream."""
from pathlib import Path
from threading import Event
from time import monotonic

from PySide6.QtWidgets import QApplication
from test_cursor_playback import finish, open_editor
from test_episode_panel import wait_until
from test_episode_roll import roll_movie as roll_movie

from sculptcut.domain import Override
from sculptcut.episode_context_preview import adjoining_scenes
from sculptcut.intro import body_start_frame
from sculptcut.output_cuts import cut_output
from sculptcut.project import save_project
from sculptcut.rendering import render, verify_output
from sculptcut.selection_draft import save_draft


def test_baked_bookend_cut_plays_across_scenes_without_preparation_or_backend_switch(roll_movie):
    app = QApplication.instance() or QApplication([])
    root, project, plan, _, video = roll_movie
    head = round(body_start_frame(project.settings, plan.output_fps,
                                  plan.actual_frame_count)*1000/plan.output_fps)
    trimmed = cut_output(video, [(0, head*1000)], Event(), lambda _: None)
    window, dialog = open_editor(root, trimmed, app)
    busy, frames = [], []
    dialog.sequence.busy_changed.connect(busy.append)
    dialog.playback.image_ready.connect(lambda image, stamp: frames.append((monotonic(), stamp)))
    try:
        dialog.seek_montage(1700)
        backend = dialog.playback.player
        window.play_button.click()
        wait_until(app, lambda: dialog.sequence.cursor >= 4000, 10)
        assert dialog.sequence.direct
        assert dialog.playback.player is backend
        assert Path(backend.source().toLocalFile()) == trimmed.resolve()
        assert dialog.sequence.preparer.worker is None and not any(busy)
        assert dialog.sequence.direct_offset == -head
        assert not dialog.playback.audio.isMuted()
        assert len(frames) >= 30
        assert max(b[0]-a[0] for a, b in zip(frames, frames[1:], strict=False)) < 0.4
        assert all(0 <= stamp < 4500 for _, stamp in frames)
        window.play_button.click()
        row, a, b = dialog.montage_seek.blocks[0]
        dialog.commit_edge(row, 'end', (a+b)//2)
        dialog.seek_montage(dialog.montage_seek.blocks[1][1]+50)
        window.play_button.click()
        wait_until(app, lambda: dialog.playback.is_advancing(), 15)
        assert not dialog.sequence.direct  # New timing needs a new music envelope.
        assert all(s.path == trimmed and s.decision is None
                   for _, _, s in dialog.sequence.entries)
        # Unchanged adjacent scenes share one decode/encode operation per window.
        scenes = tuple(s for _, _, s in dialog.sequence.entries[1:])
        assert len(adjoining_scenes(scenes)) < len(scenes)
        fps = dialog.session.plan.output_fps
        verify_output(Path(dialog.playback.player.source().toLocalFile()),
                      round(dialog.playback.end*fps/1000), fps, expected_audio=True)
    finally:
        finish(window, dialog, app)


def test_old_version_ignores_newer_project_rules_and_restores_its_own_draft(roll_movie):
    app = QApplication.instance() or QApplication([])
    root, project, plan, _, _ = roll_movie
    last = next(d for d in reversed(plan.decisions) if d.output_frame_count)
    older = plan.model_copy(update={'decisions': [
        d.model_copy(update={'action': 'HOLD', 'provenance': 'user:final_result'})
        if d is last else d for d in plan.decisions],
        'settings': plan.settings | {'source_selection': 'source-selection-277',
                                    'source_selection_baseline': '[]'}})
    video = root/'renders/old-hold.mp4'
    render(root, project, older, video, encoder='libx264')
    newer = project.model_copy(update={'overrides': [Override(source_id=last.source_id,
        start_us=last.source_start_us, end_us=last.source_end_us, action='DROP')]})
    save_project(root, newer)
    before = (root/'project.json').read_bytes()
    window, dialog = open_editor(root, video, app)
    try:
        wait_until(app, lambda: not window.video_frame_image.isNull())
        assert dialog.session.plan.decisions == older.decisions
        assert dialog.session.project.overrides == project.overrides
        assert dialog.root_overrides == newer.overrides
        row, a, b = dialog.montage_seek.blocks[0]
        dialog.commit_edge(row, 'end', (a+b)//2)
        expected = dialog.session.plan
        dialog.seek_montage(2200)
        save_draft(root, video, dialog.session, 2200, (0, 3000), newer.overrides)
        assert (root/'project.json').read_bytes() == before
    finally:
        finish(window, dialog, app)
    window, dialog = open_editor(root, video, app)
    try:
        assert dialog.session.plan == expected
        assert dialog.session.restored_cursor == 2200
        assert dialog.session.undo_stack
        assert (root/'project.json').read_bytes() == before
    finally:
        finish(window, dialog, app)
