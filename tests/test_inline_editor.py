"""Always-visible editing, reused rendered scenes, and centered selection navigation."""
from PySide6.QtWidgets import QApplication
from test_cursor_playback import finish
from test_episode_panel import wait_until
from test_episode_roll import roll_movie as roll_movie

from sculptcut.simple_timeline import SimpleTimeline
from sculptcut.ui import MainWindow


def test_editor_opens_inline_and_reuses_saved_segments_after_trim(roll_movie):
    app = QApplication.instance() or QApplication([])
    root, _, _, _, video = roll_movie
    window = MainWindow()
    dialog = None
    try:
        window.folder.setText(str(root))
        window.show()
        window.open_video(video, "test")
        wait_until(app, lambda: window.selection_dialog is not None
                   and not window.selection_dialog.loading)
        dialog = window.selection_dialog
        window.update_result_actions()
        assert dialog.isVisible() and not window.cut_button.isVisible()
        row, a, b = dialog.montage_seek.blocks[0]
        dialog.commit_edge(row, "end", (a+b)//2)
        assert dialog.session.changed
        cursor = dialog.montage_seek.blocks[1][1]+100
        dialog.seek_montage(cursor)
        window.play_button.click()
        wait_until(app, lambda: dialog.sequence.current >= 0
                   and dialog.playback.is_advancing(), 20)
        sequence = dialog.sequence
        assert not sequence.direct  # The actual montage contains the new cut.
        start, end, scene = sequence.entries[sequence.current]
        assert start == cursor
        assert scene.path == video and scene.decision is None
        # Saved-video mapping snaps to output frames before converting to milliseconds.
        assert abs(scene.start_ms - 2100) <= 1000 / dialog.session.plan.output_fps
        assert dialog.playback.end >= end-start
        assert dialog.playback.end <= sequence.maximum_window_ms
        assert dialog.playback.audio.isMuted()
        assert dialog.music_track is not None and not dialog.music_track.audio.isMuted()
        assert dialog.music_track.player.playbackState().name == 'PlayingState'
    finally:
        if dialog is not None:
            finish(window, dialog, app)
        else:
            window._allow_close = True
            window.close()


def test_follow_centers_episode_keeps_edges_visible_and_respects_mouse_pan():
    app = QApplication.instance() or QApplication([])
    track = SimpleTimeline()
    track.resize(620, 86)
    track.setRange(0, 60000)
    track.blocks = [(5, 25000, 27000)]
    try:
        track.set_view(0, 10000)
        track.follow(26500, 5)
        assert abs(track.x_at(26000)-310) <= 1
        assert 10 <= track.x_at(25000) < track.x_at(27000) <= 610
        before = track.view_start
        track.pan_x = 20
        track.follow(50000)
        assert track.view_start == before
        track.pan_x = None
        track.follow(59900)
        assert track.view_start == 50000
        assert 10 <= track.x_at(59900) <= 610
    finally:
        track.close()
        app.processEvents()
