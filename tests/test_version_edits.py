"""Version-scoped, source-addressed changes stay readable across saving and creation."""
from dataclasses import replace
from pathlib import Path
from threading import Event

from PySide6.QtWidgets import QApplication
from test_creation_edits import wait_creation
from test_cursor_playback import finish, open_editor
from test_episode_panel import wait_until
from test_episode_roll import roll_movie as roll_movie
from test_source_selection import session

from sculptcut.combined_edits import creation_edit, render_combined_edit
from sculptcut.domain import Override
from sculptcut.edit_stats import _episode_count
from sculptcut.project import load_project, save_project
from sculptcut.rendering import verify_output
from sculptcut.selection_history import edit_origin, scoped_edits
from sculptcut.source_selection import SourceSelection, merge_edit_rules, put_rule
from sculptcut.time_format import format_time
from sculptcut.ui import MainWindow
from sculptcut.variants import read_variant


def marks(edit, all_versions=False):
    return scoped_edits(edit.project, edit.base, edit.reference, edit.plan, all_versions)


def test_exact_green_and_gray_edges_restore_independently_and_keep_timestamp():
    edit = session()
    edit.resize(4, 11_000_000, 14_000_000, 2)
    changed = marks(edit)
    assert [(e.start_us, e.end_us, e.kind) for e in changed] == [
        (11_000_000, 12_000_000, 'added'), (14_000_000, 15_000_000, 'removed')]
    assert all(not e.inherited for e in changed)
    origin = edit_origin(edit.plan, changed[0])
    assert origin['version'] == 'original.mp4' and origin['created_at'].endswith('+00:00')
    snapshot = edit.plan
    edit.restore_manual(changed[0])
    assert [(e.start_us, e.kind) for e in marks(edit)] == [(14_000_000, 'removed')]
    edit.undo()
    assert edit.plan == snapshot and edit_origin(edit.plan, marks(edit)[0]) == origin
    edit.undo(redo=True)
    edit.restore_manual(marks(edit)[0])
    assert marks(edit) == []


def test_new_version_hides_inherited_edits_and_restoring_old_cut_is_a_new_addition():
    first = session()
    first.resize(4, 11_000_000, 14_000_000, 2)
    second = SourceSelection(replace(first.export_edit(), parent=Path('second.mp4')))
    second.reference = first.reference
    assert marks(second) == []
    inherited = marks(second, True)
    assert len(inherited) == 2 and all(e.inherited for e in inherited)
    cut = next(e for e in inherited if e.kind == 'removed')
    second.restore_manual(cut)
    current = marks(second)
    assert [(e.start_us, e.end_us, e.kind) for e in current] == [
        (14_000_000, 15_000_000, 'added')]
    assert edit_origin(second.plan, current[0])['version'] == 'second.mp4'
    assert any(e.inherited and e.start_us == 11_000_000 for e in marks(second, True))


def test_create_merges_only_pending_version_delta_without_accumulating_rules():
    edit = session()
    unrelated = Override(source_id='0', start_us=30_000_000, end_us=31_000_000,
                         action='DROP', exact_selection=True)
    global_project = put_rule(edit.project, unrelated)
    before = edit.project.overrides
    edit.resize(4, 11_000_000, 14_000_000, 2)
    merged = merge_edit_rules(global_project, before, edit.project.overrides)
    assert unrelated in merged.overrides
    assert merge_edit_rules(merged, before, edit.project.overrides) == merged
    # A fresh creation inherits exclusions and dates, but owns a clean current layer.
    result = creation_edit(merged, edit.base, edit.export_edit())
    assert (result.plan.settings['selection_edit_journal']
            == edit.plan.settings['selection_edit_journal'])
    second = SourceSelection(replace(result, parent=Path('created.mp4')))
    assert marks(second) == []
    assert any(not d.output_frame_count and d.source_start_us == 14_000_000
               for d in second.plan.decisions)
    # Removing one inherited rule must not clear another version's exclusions.
    restored = merge_edit_rules(merged, edit.project.overrides, [])
    assert restored.overrides == [unrelated]


def test_native_colored_edits_statistics_draft_reopen_and_short_export(roll_movie):
    app = QApplication.instance() or QApplication([])
    root, project, _, _, video = roll_movie
    original = video.read_bytes()
    window, dialog = open_editor(root, video, app)
    try:
        dialog.session.resize(4, 11_000_000, 14_000_000, 2)
        dialog.refresh()
        assert _episode_count(dialog.session.plan) == dialog.montage.count()
        assert window.statistics.isVisible()
        assert f'Эпизодов: {dialog.montage.count()}' in window.statistics.text()
        assert f'Ролик: {format_time(5.5)}' in window.statistics.text()
        changes = dialog.montage_seek.edits
        assert {m.kind for m in changes} == {'removed', 'added'}
        assert all('original.mp4' in m.detail for m in changes)
        green = next(m for m in changes if m.kind == 'added')
        gray = next(m for m in changes if m.removed)
        image = dialog.montage_seek.grab().toImage()
        x = round(sum(dialog.montage_seek.edit_bounds(green))/2)
        color = image.pixelColor(x, dialog.montage_seek.marker_y)
        assert color.green() > color.red()+30
        left, right = dialog.montage_seek.edit_bounds(gray)
        assert right-left >= 14
        dialog.edit_marked(gray.key)
        assert any(e.kind == 'removed' for e in dialog.timeline.edits)
        assert all(e.source_id == dialog.sources.currentData() for e in dialog.timeline.edits)
        assert dialog.timeline.isVisible() and not dialog.montage_seek.isVisible()
        dialog.close_manual_edit()
        window.save_trim_draft(force=True)
        expected, origin = dialog.session.plan, changes[0].detail
    finally:
        finish(window, dialog, app)

    window, dialog = open_editor(root, video, app)
    try:
        assert dialog.session.plan == expected
        assert dialog.montage_seek.edits[0].detail == origin
        output = root/'previews/version-edits.mp4'
        render_combined_edit(root, dialog.session.export_edit(), output, Event(), lambda _: None,
                             encoder='libx264', preview=True, operation='source_selection')
        verify_output(output, 165, 30, expected_audio=True)
        updated_project = dialog.session.project
    finally:
        finish(window, dialog, app)
    save_project(root, updated_project)
    window, dialog = open_editor(root, output, app)
    try:
        assert dialog.manual_ranges == [] and dialog.edit_scope.text() == 'Правки версии: 0 ▾'
        dialog.set_edit_scope(True)
        assert len(dialog.manual_ranges) == 2 and all(e.inherited for e in dialog.manual_ranges)
        cut = next(m for m in dialog.montage_seek.edits if m.removed)
        dialog.restore_marked(cut.key)
        dialog.set_edit_scope(False)
        assert len(dialog.manual_ranges) == 1 and dialog.manual_ranges[0].kind == 'added'
        assert 'version-edits.mp4' in dialog.montage_seek.edits[0].detail
        assert _episode_count(dialog.session.plan) == dialog.montage.count()
        assert video.read_bytes() == original and project.settings.target_seconds == 5.5
        artifacts = Path('artifacts/version-edits-305')
        artifacts.mkdir(parents=True, exist_ok=True)
        window.cut_section.show()
        app.processEvents()
        window.grab().save(str(artifacts/'native-version-edits.png'))
    finally:
        finish(window, dialog, app)


def test_real_create_includes_unsaved_inline_delta_once_and_keeps_other_exclusions(
    roll_movie, monkeypatch,
):
    app = QApplication.instance() or QApplication([])
    root, project, _, _, video = roll_movie
    project = project.model_copy(update={'settings': project.settings.model_copy(update={
        'target_seconds': 6, 'expressive_pacing': False, 'proxy_width': 320, 'proxy_fps': 5})})
    # This newer global exclusion is deliberately absent from the selected old video.
    unrelated = Override(source_id=project.sources[0].id, start_us=30_000_000,
                         end_us=31_000_000, action='DROP', exact_selection=True)
    project = put_rule(project, unrelated)
    save_project(root, project)
    window = MainWindow()
    errors = []
    monkeypatch.setattr(window, 'show_error', errors.append)
    try:
        window.show()
        window.open_project_folder(root)
        window.open_video(video, 'test')
        wait_until(app, lambda: window.selection_dialog is not None
                   and not window.selection_dialog.loading)
        dialog = window.selection_dialog
        dialog.session.resize(4, 11_000_000, 14_000_000, 2)
        dialog.refresh()
        window.output_name.setText('VersionEdits')
        window.target.setValue(6 if window.duration_in_seconds else 0.1)
        previous_rules = None
        for _ in range(2):
            window.create_button.click()
            assert window.job is not None, errors
            wait_creation(window)
            output = window.last_result_video
            state = read_variant(output)
            rules = load_project(root).overrides
            assert unrelated in rules
            assert any(o.action == 'DROP' and o.start_us == 14_000_000
                       and o.end_us == 15_000_000 for o in rules)
            if previous_rules is not None:
                assert rules == previous_rules
            previous_rules = rules
            assert 'selection_edit_journal' in state['plan']['settings']
            assert all(not d['output_frame_count'] for d in state['plan']['decisions']
                       if d['source_start_us'] >= 14_000_000 and d['source_end_us'] <= 15_000_000)
            verify_output(output, 180, 30, expected_audio=True)
        assert not errors
    finally:
        if window.job is not None:
            window.job.cancel.set()
            window.job.wait(30_000)
        finish(window, dialog, app)
