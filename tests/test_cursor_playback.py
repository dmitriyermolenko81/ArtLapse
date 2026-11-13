"""Cursor playback follows edited scenes while collapsed trimming keeps its session."""
from pathlib import Path

from PySide6.QtWidgets import QApplication
from test_episode_panel import wait_until
from test_episode_roll import roll_movie as roll_movie

from sculptcut.ui import MainWindow


def open_editor(root, video, app):
    window = MainWindow()
    window.folder.setText(str(root))
    window.show()
    window.open_video(video, "test")
    window.player.pause()
    window.cut_button.click()
    dialog = window.selection_dialog
    wait_until(app, lambda: not dialog.loading)
    return window, dialog


def finish(window, dialog, app):
    window._allow_close = True
    window.close()
    wait_until(app, lambda: dialog.preparer.cleaned and dialog.sequence.preparer.cleaned)
    app.processEvents()


def test_cursor_and_episode_modes_keep_edits_when_panel_collapses(roll_movie):
    app = QApplication.instance() or QApplication([])
    root, _, _, _, video = roll_movie
    window, dialog = open_editor(root, video, app)
    before = (root / "project.json").read_bytes()
    clocks = []
    try:
        row, a, b = dialog.montage_seek.blocks[0]
        dialog.commit_edge(row, "end", (a+b)//2)
        edited = dialog.session.plan
        assert dialog.session.changed
        cursor = dialog.montage_seek.blocks[1][1] + 100
        dialog.seek_montage(cursor)
        dialog.sequence.clock_changed.connect(clocks.append)
        window.play_button.click()
        wait_until(app, lambda: dialog.sequence.current >= 0 and dialog.playback.is_advancing(), 20)
        assert dialog.sequence.enabled and not dialog.sequence.episode_only
        assert window.status.text() != "Готовим просмотр с курсора…"
        assert dialog.sequence.initial_cursor == cursor
        assert Path(dialog.playback.player.source().toLocalFile()).name == "context.mp4"
        assert dialog.playback.audio.isMuted()
        assert dialog.music_track is not None and not dialog.music_track.audio.isMuted()
        assert dialog.music_track.player.playbackState().name == 'PlayingState'
        window.cut_button.click()
        assert window.selection_dialog is dialog and not dialog.isVisible()
        assert dialog.session.plan == edited
        wait_until(app, lambda: bool(clocks) and clocks[-1] >= dialog.montage_seek.blocks[2][1], 15)
        window.play_button.click()
        assert not dialog.sequence.desired
        window.cut_button.click()
        assert dialog.isVisible() and dialog.session.plan == edited
        dialog.seek_montage(cursor)
        dialog.play_montage(episode_only=True)  # Internal compatibility; one public play button.
        wait_until(app, lambda: dialog.sequence.current >= 0 and dialog.playback.is_advancing(), 20)
        assert dialog.sequence.episode_only
        wait_until(app, lambda: not dialog.sequence.desired, 10)
        assert dialog.sequence.cursor <= dialog.montage_seek.blocks[1][2] + 50
        assert dialog.session.plan == edited
        assert (root / "project.json").read_bytes() == before
        assert dialog.apply.text() == "Применить правки"
        dialog.seek_montage(0)
        window.play_button.click()
        wait_until(app, lambda: dialog.sequence.current == 0
                   and dialog.playback.is_advancing(), 20)
        assert dialog.sequence.entries[0][2].path == video
        assert dialog.sequence.initial_cursor == 0
        window.play_button.click()
    finally:
        finish(window, dialog, app)


def test_continuous_chunks_prefetch_and_stop_without_orphan_audio(roll_movie):
    app = QApplication.instance() or QApplication([])
    root, _, _, _, video = roll_movie
    window, dialog = open_editor(root, video, app)
    try:
        plan = dialog.session.plan
        decisions = [d.model_copy(update={"action": "SPEED", "output_frame_count": 18})
                     for d in plan.decisions]
        plan = plan.model_copy(update={"decisions": decisions,
            "actual_frame_count": sum(d.output_frame_count for d in decisions),
            "target_frame_count": sum(d.output_frame_count for d in decisions)})
        dialog.session.save(plan, dialog.session.project)
        dialog.refresh()
        dialog.play_montage()
        wait_until(app, lambda: dialog.sequence.current >= 0
                   and dialog.playback.is_advancing(), 20)
        first = dialog.sequence.current
        wait_until(app, lambda: dialog.sequence.current > first, 25)
        assert dialog.sequence.current-first <= dialog.sequence.maximum_window_scenes
        assert len(dialog.sequence.entries) == len(decisions) + 1
        assert dialog.playback.end <= dialog.sequence.maximum_window_ms + 1
        dialog.sequence.toggle()
        assert not dialog.playback.want_play
        assert not dialog.sequence.desired
        dialog.seek_montage(dialog.montage_seek.blocks[2][1])
        assert not dialog.sequence.enabled and not dialog.playback.want_play
        assert dialog.playback.audio.isMuted()
    finally:
        finish(window, dialog, app)

def test_apply_failure_keeps_edit_session_reviewable(roll_movie, monkeypatch):
    import sculptcut.combined_edits as combined

    app = QApplication.instance() or QApplication([])
    root, _, _, _, video = roll_movie
    window, dialog = open_editor(root, video, app)
    errors = []
    monkeypatch.setattr(window, "show_error", errors.append)
    def fail(*args, **kwargs):
        raise ValueError("test failure")
    monkeypatch.setattr(combined, "render_combined_edit", fail)
    try:
        row, a, b = dialog.montage_seek.blocks[0]
        dialog.commit_edge(row, "end", (a+b)//2)
        edited = dialog.session.plan
        dialog.apply.click()
        wait_until(app, lambda: window.job is None)
        assert errors and "test failure" in errors[0]
        assert window.selection_dialog is dialog and dialog.isEnabled()
        assert dialog.session.plan == edited and not dialog.closed
        window.cut_button.click()
        window.cut_button.click()
        assert dialog.isVisible() and dialog.session.plan == edited
    finally:
        finish(window, dialog, app)
