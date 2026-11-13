"""Fixed duration and source-addressed manual reversions survive retiming and restart."""
from pathlib import Path
from threading import Event

import numpy as np
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu
from shiboken6 import isValid
from test_cursor_playback import finish, open_editor
from test_episode_panel import wait_until
from test_episode_roll import roll_movie as roll_movie
from test_timeline import make_source

from sculptcut.combined_edits import CombinedEdit, prepare_combined_edit, render_combined_edit
from sculptcut.domain import Project, ProjectSettings
from sculptcut.media import discover, run_process
from sculptcut.planning import RenderDecision, build_plan, validate_plan
from sculptcut.selection_duration import selection_frames, timeline_frames
from sculptcut.selection_history import manual_edits
from sculptcut.source_selection import SourceSelection


def test_cut_30_seconds_refits_remaining_windows_to_five_minutes_with_one_undo(tmp_path):
    project = Project(sources=[make_source(0, 0, 1_200_000_000)],
                      settings=ProjectSettings(target_seconds=300, final_view_seconds=0))
    decisions = [RenderDecision(source_id='0', source_start_us=i*120_000_000,
        source_end_us=(i+1)*120_000_000, action='SPEED' if i % 2 == 0 else 'DROP',
        output_frame_count=1800 if i % 2 == 0 else 0, provenance='story:work_highlight')
        for i in range(10)]
    plan = build_plan(project, [], 300).model_copy(update={'decisions': decisions,
        'actual_frame_count': 9000, 'target_frame_count': 9000})
    edit = SourceSelection(CombinedEdit(project, project, plan, plan, Path('old.mp4'), [], [], []),
                           fixed_duration=True)
    edit.select('0', 60_000_000, 120_000_000, None)  # Exactly 30 output seconds.
    assert selection_frames(edit.project, edit.plan, [], []) == 9000
    assert len(edit.undo_stack) == 1
    assert any(d.source_start_us == 60_000_000 and d.source_end_us == 120_000_000
               and not d.output_frame_count for d in edit.plan.decisions)
    # Retime changes counts, never the retained content or speed-ramp shape.
    assert all(not d.output_frame_count for d in edit.plan.decisions
               if d.source_start_us >= 120_000_000 and d.source_end_us <= 240_000_000)
    validate_plan(edit.project, edit.plan)
    first = manual_edits(edit.project, edit.reference, edit.plan)[0]
    edit.select('0', 300_000_000, 360_000_000, None)
    edit.restore_manual(first)
    assert not any(o.start_us == first.start_us and o.action == 'DROP'
                   for o in edit.project.overrides)
    assert any(o.start_us == 300_000_000 and o.action == 'DROP' for o in edit.project.overrides)
    assert edit.plan.actual_frame_count == 9000
    edit.undo()
    assert any(o.start_us == first.start_us for o in edit.project.overrides)
    edit.undo(redo=True)
    assert edit.plan.actual_frame_count == 9000
    # Old files stored exact rules without a clean pre-edit layer. Recover
    # those user cuts, keeping unrelated analytical omissions excluded.
    legacy = SourceSelection(CombinedEdit(edit.project, edit.project, edit.plan,
        edit.plan, Path('legacy.mp4'), [], [], []), fixed_duration=True)
    cut = next(e for e in manual_edits(legacy.project, legacy.reference, legacy.plan)
               if e.action == 'DROP')
    legacy.restore_manual(cut)
    assert legacy.plan.actual_frame_count == 9000
    assert not any(o.start_us == cut.start_us and o.action == 'DROP'
                   for o in legacy.project.overrides)
    assert all(not d.output_frame_count for d in legacy.plan.decisions
               if d.source_start_us >= 120_000_000 and d.source_end_us <= 240_000_000)
    from test_intro import make_image

    cover = tmp_path/'cover.png'
    make_image(cover, (50, 80, 30))
    tiny_project = project.model_copy(update={'settings': project.settings.model_copy(update={
        'target_seconds': 3, 'intro_image': cover, 'intro_seconds': 2,
        'intro_transition_seconds': 1.5})})
    tiny_plan = build_plan(tiny_project, [], 3).model_copy(update={'decisions': [
        d.model_copy(update={'output_frame_count': 10 if i == 0 else 0,
                             'action': 'SPEED' if i == 0 else 'DROP'})
        for i, d in enumerate(plan.decisions)], 'actual_frame_count': 10,
        'target_frame_count': 10})
    tiny = SourceSelection(CombinedEdit(tiny_project, tiny_project, tiny_plan, tiny_plan,
                                       Path('tiny.mp4'), [], [], []), fixed_duration=True)
    tiny.fit_duration()
    assert selection_frames(tiny.project, tiny.plan, [], []) == 90


def test_inline_manual_marks_revise_and_restore_independently_after_restart_and_export(roll_movie):
    app = QApplication.instance() or QApplication([])
    root, project, _, _, video = roll_movie
    original = video.read_bytes()
    window, dialog = open_editor(root, video, app)
    try:
        for row in (0, 1):
            _, a, b = dialog.montage_seek.blocks[row]
            dialog.commit_edge(row, 'end', (a+b)//2)
        assert timeline_frames(dialog.session.project, dialog.session.plan, [], []) == 165
        assert dialog.montage_seek.maximum() == 5499
        cuts = [m for m in dialog.montage_seek.edits if m.removed]
        assert len(cuts) == 2
        first, second = [dialog.manual_ranges[m.key] for m in cuts]
        context_seen = []
        def restore_from_menu():
            menus = dialog.montage_seek.findChildren(QMenu)
            stage = 'root'
            try:
                menu = dialog.montage_seek.edit_menu
                stage = 'root-actions'
                root_action = menu.actions()[0]
                child = root_action.menu()
                command = child.actions()[0] if child else root_action
                stage = f'trigger:{isValid(command)}'
                command.trigger()
                context_seen.append('restored')
            except Exception as exc:
                context_seen.append(stage+': '+str(exc))
            finally:
                for menu in reversed(menus):
                    if isValid(menu):
                        menu.close()
        QTimer.singleShot(50, restore_from_menu)
        QTest.mouseClick(dialog.montage_seek, Qt.MouseButton.RightButton,
            pos=QPoint(round(dialog.montage_seek.x_at(cuts[0].start_ms)),
                       dialog.montage_seek.marker_y))
        assert context_seen == ['restored']
        assert any(r.start_us == second.start_us and r.action == 'DROP'
                   for r in dialog.manual_ranges)
        assert not any(r.start_us == first.start_us and r.action == 'DROP'
                       for r in dialog.manual_ranges)
        cut = next(m for m in dialog.montage_seek.edits if m.removed)
        assert dialog.montage_seek.edit_at(
            dialog.montage_seek.x_at(cut.start_ms), dialog.montage_seek.marker_y) == cut
        dialog.edit_marked(cut.key)
        assert dialog.timeline.isVisible() and not dialog.montage_seek.isVisible()
        assert dialog.manual_tools.isVisible() and not dialog.apply.isVisible()
        dialog.timeline.start += (dialog.timeline.end-dialog.timeline.start)//2
        wanted = dialog.timeline.start
        dialog.save_open_edit()
        assert not dialog.manual_tools.isVisible() and dialog.montage_seek.isVisible()
        assert any(r.start_us == wanted and r.action == 'DROP' for r in dialog.manual_ranges)
        assert not dialog.timeline.isVisible()
        assert timeline_frames(dialog.session.project, dialog.session.plan, [], []) == 165
        dialog.seek_montage(dialog.montage_seek.blocks[1][1]+100)
        assert dialog.saved_scrub  # Retime must not reopen hours of source for scrubbing.
        window.save_trim_draft(force=True)
        expected = dialog.session.plan
    finally:
        finish(window, dialog, app)
    legacy = SourceSelection(prepare_combined_edit(root, video,
        apply_current=False, exact_variant=True))
    legacy.select(project.sources[0].id, 1_500_000, 3_000_000, None)
    legacy_video = root/'previews/legacy-short.mp4'
    render_combined_edit(root, legacy.export_edit(), legacy_video, Event(), lambda _: None,
                         encoder='libx264', preview=True, operation='source_selection')
    window, dialog = open_editor(root, legacy_video, app)
    try:
        assert dialog.session.changed  # Legacy shortened edits become a fitted draft on load.
        assert timeline_frames(dialog.session.project, dialog.session.plan, [], []) == 165
        dialog.set_edit_scope(True)  # Saved edits belong to this version's history.
        assert any(m.removed for m in dialog.montage_seek.edits)
        assert all(not d.output_frame_count for d in dialog.session.plan.decisions
                   if d.source_start_us >= 1_500_000 and d.source_end_us <= 3_000_000)
    finally:
        finish(window, dialog, app)
    window, dialog = open_editor(root, video, app)
    try:
        assert dialog.session.plan == expected
        assert any(r.start_us == wanted and r.action == 'DROP' for r in dialog.manual_ranges)
        dialog.play_montage()
        wait_until(app, dialog.playback.is_advancing, 20)
        assert all(s.path == video and s.decision is None for _, _, s in dialog.sequence.entries)
        dialog.sequence.stop()
        output = root/'previews/fixed-selection.mp4'
        render_combined_edit(root, dialog.session.export_edit(), output, Event(), lambda _: None,
                             encoder='libx264', preview=True, operation='source_selection')
        from sculptcut.rendering import verify_output

        verify_output(output, 165, 30, expected_audio=True)
        from sculptcut.project import save_project

        save_project(root, dialog.session.project)
        final = prepare_combined_edit(root, output)
        assert selection_frames(final.project, final.plan, final.leading, final.trailing) == 165
        # A new variant carries the neutral layer for independent recovery later.
        window.open_video(output, 'test')
        wait_until(app, lambda: window.selection_dialog is not None
                   and not window.selection_dialog.loading)
        dialog = window.selection_dialog
        dialog.set_edit_scope(True)
        assert any(r.start_us == wanted and r.action == 'DROP' for r in dialog.manual_ranges)
        pcm = root/'audio.f32'
        run_process([discover('ffmpeg'), '-v', 'error', '-i', str(output), '-vn',
                     '-ac', '1', '-ar', '48000', '-f', 'f32le', str(pcm)])
        samples = np.fromfile(pcm, dtype='<f4')
        def rms(a):
            return float(np.sqrt(np.mean(a*a)))
        assert rms(samples[-9600:]) < rms(samples[48000:57600])*0.1
        cut = next(m for m in dialog.montage_seek.edits if m.removed)
        dialog.restore_marked(cut.key)
        assert not any(r.action == 'DROP' and r.start_us == wanted for r in dialog.manual_ranges)
        assert timeline_frames(dialog.session.project, dialog.session.plan, [], []) == 165
        assert video.read_bytes() == original
        assert project.settings.target_seconds == 5.5
    finally:
        finish(window, dialog, app)


def test_baked_intro_cut_counts_inside_target_without_lengthening_the_final_hold(roll_movie):
    from sculptcut.intro import body_start_frame
    from sculptcut.output_cuts import cut_output
    from sculptcut.rendering import render, verify_output

    root, project, plan, _, _ = roll_movie
    last = next(d for d in reversed(plan.decisions) if d.output_frame_count)
    plan = plan.model_copy(update={'decisions': [d.model_copy(update={
        'action': 'HOLD', 'provenance': 'user:final_result'}) if d is last else d
        for d in plan.decisions]})
    video = root/'renders/hold.mp4'
    render(root, project, plan, video, encoder='libx264')
    head = body_start_frame(project.settings, plan.output_fps, plan.actual_frame_count)
    trimmed = cut_output(video, [(0, head*1_000_000//plan.output_fps)], Event(), lambda _: None)
    prepared = prepare_combined_edit(root, trimmed, apply_current=False, exact_variant=True)
    edit = SourceSelection(prepared, fixed_duration=True)
    edit.select(project.sources[0].id, 1_500_000, 3_000_000, None)
    assert timeline_frames(edit.project, edit.plan, edit.original.leading, []) == 165
    assert next(d.output_frame_count for d in edit.plan.decisions if d.action == 'HOLD') == 60
    output = root/'previews/hold-fixed.mp4'
    render_combined_edit(root, edit.export_edit(), output, Event(), lambda _: None,
                         encoder='libx264', preview=True)
    verify_output(output, 165, 30, expected_audio=True)
