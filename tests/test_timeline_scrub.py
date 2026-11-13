"""Scene navigation decodes the saved render, with original footage only for new edges."""

from PySide6.QtWidgets import QApplication
from test_cursor_playback import finish
from test_episode_panel import wait_until
from test_episode_roll import roll_movie as roll_movie

from sculptcut.ui import MainWindow


def test_saved_scene_seek_reuses_decoder_and_maps_trimmed_output_clock(roll_movie):
    app = QApplication.instance() or QApplication([])
    root, _, _, _, video = roll_movie
    window = MainWindow()
    dialog = None
    try:
        window.folder.setText(str(root))
        window.show()
        window.open_video(video, 'test')
        wait_until(app, lambda: window.selection_dialog is not None
                   and not window.selection_dialog.loading)
        dialog = window.selection_dialog
        frames = []
        dialog.scrub_frames.ready.connect(lambda key, image: frames.append(key[1]//1000))
        changes = []
        dialog.playback.player.sourceChanged.connect(changes.append)
        for row in (1, 2, 0, 1):
            cursor = dialog.montage_seek.blocks[row][1] + 100
            frames.clear()
            dialog.seek_montage(cursor)
            wait_until(app, lambda: bool(frames), 8)
            assert abs(frames[-1]-cursor) < 100
            assert dialog.scrub_frames.wanted[0] == video.resolve()
            assert dialog.saved_scrub and not dialog.playback.want_play
            assert dialog.images.worker is None and not dialog.images.pending
        assert not changes  # Opening the editor already loaded this same saved video.
        cursor = dialog.montage_seek.value()
        window.play_button.click()
        wait_until(app, lambda: dialog.playback.is_advancing(), 10)
        assert dialog.sequence.direct and dialog.sequence.initial_cursor == cursor
        window.play_button.click()
        window.play_button.click()
        assert dialog.sequence.desired
        window.play_button.click()
        assert not dialog.sequence.desired

        row, a, b = dialog.montage_seek.blocks[0]
        old_next = dialog.montage_seek.blocks[1][1]
        dialog.commit_edge(row, 'end', (a+b)//2)
        new_next = dialog.montage_seek.blocks[1][1]
        assert new_next < old_next
        frames.clear()
        dialog.seek_montage(new_next + 100)
        wait_until(app, lambda: bool(frames), 8)
        assert abs(frames[-1]-(old_next+100)) < 100
        assert dialog.montage_seek.value() == new_next+100
        assert dialog.saved_scrub and dialog.images.worker is None
        decision = dialog.session.plan.decisions[dialog.active_index]
        dialog.scrub_source(decision.source_start_us-100_000)
        wait_until(app, lambda: not dialog.scrub_timer.isActive())
        assert not dialog.saved_scrub
        assert dialog.scrub_frames.wanted[0] != video.resolve()
        dialog.seek_montage(new_next+100)
        assert dialog.saved_scrub
        assert dialog.session.changed
    finally:
        if dialog is not None:
            finish(window, dialog, app)
        else:
            window._allow_close = True
            window.close()
