"""Music-folder snapshots must use the saved soundtrack fast path; versions stay visible."""
import json
import shutil
from pathlib import Path

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QApplication
from test_cursor_playback import finish
from test_episode_panel import wait_until
from test_episode_roll import roll_movie as roll_movie

from sculptcut.export_storage import metadata_path
from sculptcut.ui import MainWindow
from sculptcut.variants import read_variant


def test_music_folder_fast_path_and_version_switch_preserve_edits(roll_movie):
    app = QApplication.instance() or QApplication([])
    root, _, _, _, video = roll_movie
    state = read_variant(video)
    # Reproduce a real music-library render (saved audio path remains in state['music']).
    state['project']['settings']['music_folder'] = str(root / 'music-library')
    state['project']['settings']['music_path'] = None
    metadata_path(video, '.variant.json').write_text(json.dumps(state), encoding='utf-8')
    other = video.with_name("alternative.mp4")
    shutil.copy2(video, other)
    metadata_path(other, ".variant.json").write_text(json.dumps(state), encoding="utf-8")
    window = MainWindow()
    dialog = None
    try:
        window.folder.setText(str(root))
        window.show()
        window.open_video(video, 'test')
        wait_until(app, lambda: window.selection_dialog is not None
                   and not window.selection_dialog.loading)
        dialog = window.selection_dialog
        assert window.versions_panel.isVisible() and window.versions_panel.isEnabled()
        assert window.variants.count() > 0
        assert dialog.saved_video_compatible
        dialog.seek_montage(2200)
        window.play_button.click()
        wait_until(app, lambda: dialog.playback.is_advancing(), 10)
        assert dialog.sequence.direct and dialog.sequence.preparer.worker is None
        assert Path(dialog.playback.player.source().toLocalFile()) == video.resolve()
        # Selecting the current version must stop its player, not start hidden
        # main-player audio behind the inline editor or restore old form values.
        states = []
        window.player.playbackStateChanged.connect(states.append)
        before = (root / 'project.json').read_bytes()
        window.target.setValue(9)
        window.variants.activated.emit(window.variants.currentIndex())
        loop = QEventLoop()
        QTimer.singleShot(350, loop.quit)
        loop.exec()
        assert window.selection_dialog is dialog
        assert not dialog.sequence.desired and not dialog.playback.want_play
        assert dialog.playback.audio.isMuted()
        assert window.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState
        assert QMediaPlayer.PlaybackState.PlayingState not in states
        assert window.target.value() == 9
        assert (root / 'project.json').read_bytes() == before
        window.restore_variant()
        assert window.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState
        assert QMediaPlayer.PlaybackState.PlayingState not in states
        # A trim should still reuse saved pictures; the music folder must not disable it.
        row, a, b = dialog.montage_seek.blocks[0]
        dialog.commit_edge(row, 'end', (a+b)//2)
        assert dialog.saved_segments_compatible
        cursor = dialog.montage_seek.blocks[1][1] + 100
        dialog.seek_montage(cursor)
        window.play_button.click()
        wait_until(app, lambda: dialog.sequence.current >= 0 and dialog.playback.is_advancing(), 20)
        assert dialog.sequence.entries[dialog.sequence.current][2].path == video
        assert window.versions_panel.isVisible() and window.versions_panel.isEnabled()
        edited = dialog.session.plan
        window.variants.activated.emit(window.variants.currentIndex())
        assert dialog.session.plan == edited and not dialog.sequence.desired
        old_dialog = dialog
        window.variants.activated.emit(window.variants.findData(str(other)))
        wait_until(app, lambda: window.selection_dialog is not None
                   and window.selection_dialog is not old_dialog
                   and not window.selection_dialog.loading)
        dialog = window.selection_dialog
        assert dialog.video == other
        wait_until(app, lambda: old_dialog.sequence.preparer.cleaned)
        window.variants.activated.emit(window.variants.findData(str(video)))
        wait_until(app, lambda: window.selection_dialog is not None
                   and window.selection_dialog.video == video
                   and not window.selection_dialog.loading)
        dialog = window.selection_dialog
        assert dialog.session.plan == edited
        assert window.versions_panel.isVisible() and window.versions_panel.isEnabled()
    finally:
        if dialog is not None:
            finish(window, dialog, app)
        else:
            window._allow_close = True
            window.close()
