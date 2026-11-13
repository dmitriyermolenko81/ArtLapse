"""Audition the edited scene between its real draft neighbours, without export."""

from pathlib import Path
from time import monotonic

import pytest
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from test_episode_panel import wait_until
from test_episode_roll import roll_movie as roll_movie
from test_episode_roll import wait_loaded

from sculptcut.episode_roll_dialog import EpisodeRollDialog
from sculptcut.rendering import verify_output


def test_context_plays_real_neighbours_replacement_and_music_without_changing_plan(roll_movie):
    app = QApplication.instance() or QApplication([])
    root, project, plan, episodes, video = roll_movie
    owner = QWidget()
    dialog = EpisodeRollDialog(root, video, owner)
    before = {p:p.read_bytes() for p in (video, root/"project.json", root/"render_plan.json")}
    frames, music, switches = [], [], []
    try:
        dialog.show()
        wait_loaded(dialog)
        dialog.select_slot(1)
        dialog.step(1)
        choices = list(dialog.roll.choices)
        snapshot = dialog.export_edit().plan
        assert choices[1].episode_id == episodes[5].id
        dialog.playback.image_ready.connect(lambda image, stamp: frames.append(
            (dialog.playback_row, stamp, image.pixelColor(10, 10), monotonic())))
        dialog.playback.player.sourceChanged.connect(lambda url: switches.append(url.toLocalFile()))
        dialog.music.positionChanged.connect(lambda stamp: music.append(stamp))
        dialog.context_play.click()
        assert dialog.context_rows == [0, 1, 2]
        wait_until(app, lambda: dialog.context_ready and bool(frames))
        clip = dialog.context_clip
        assert clip is not None and clip.path.is_file()
        verify_output(clip.path, 135, 30, expected_audio=False)
        modified = clip.path.stat().st_mtime_ns
        frames.clear()
        dialog.play.click()
        QTest.qWait(150)
        assert not dialog.playback.want_play and dialog.context_rows == [0, 1, 2]
        dialog.play.click()
        assert dialog.playback.want_play
        wait_until(app, lambda: not dialog.context_rows)
        assert dialog.row == dialog.playback_row == 1
        assert dialog.strip.currentRow() == 1 and not dialog.playback.want_play
        assert dialog.roll.choices == choices and dialog.export_edit().plan == snapshot
        assert [p.read_bytes() for p in before] == list(before.values())
        sequence = []
        for row, stamp, color, _wall in frames:
            if not sequence or sequence[-1] != row:
                sequence.append(row)
            if row == 1:
                assert 1000 <= stamp < 2500
                assert color.blue() > 75  # Sixth source episode, not saved fifth.
            else:
                a, b, _ = clip.spans[row]
                assert a <= stamp < b
        assert sequence == [0, 1, 2]
        # No decoder reopen or black/loading interval at the two edit boundaries.
        assert [Path(p) for p in switches] == [
            clip.path.resolve(), project.sources[0].path.resolve()]
        for left, right in zip(frames, frames[1:], strict=False):
            if left[0] != right[0]:
                assert right[3]-left[3] < 0.3
        assert dialog.context_preparer.worker is None
        assert music and max(music) > dialog.video_spans[2][0]+300
        assert dialog.music.source().toLocalFile().endswith((".wav", ".flac"))
        assert not dialog.context_status.isVisible()
        QTest.qWait(100)
        assert dialog.music.playbackState() != QMediaPlayer.PlaybackState.PlayingState
        stopped = dialog.music.position()
        QTest.qWait(150)
        assert abs(dialog.music.position()-stopped) < 30
        assert dialog.music_audio.volume() == 0
        dialog.toggle_context()
        wait_until(app, lambda: dialog.context_ready)
        assert dialog.context_clip.path == clip.path
        assert clip.path.stat().st_mtime_ns == modified  # Reuse the prepared window.
        dialog.context_play.click()
        assert not dialog.context_rows
    finally:
        dialog.shutdown()
        wait_until(app, lambda: dialog.context_preparer.cleaned)
        dialog.close()
        owner.close()
        app.processEvents()


@pytest.mark.parametrize("row,expected", [(0, [0, 1]), (2, [1, 2])])
def test_context_edges_cancel_queued_advance_and_seek(roll_movie, row, expected):
    app = QApplication.instance() or QApplication([])
    root, _, _, _, video = roll_movie
    owner = QWidget()
    dialog = EpisodeRollDialog(root, video, owner)
    try:
        dialog.show()
        wait_loaded(dialog)
        dialog.select_slot(row)
        dialog.toggle_context()
        assert dialog.context_rows == expected
        # A scene end already queued must not restart playback after Stop.
        dialog.scene_finished()
        dialog.context_play.click()
        app.processEvents()
        assert not dialog.context_rows and not dialog.playback.want_play
        assert dialog.row == dialog.playback_row == row
        dialog.toggle_context()
        dialog.seek(500)
        app.processEvents()
        assert not dialog.context_rows and not dialog.playback.want_play
        assert dialog.playback.seek_target == (dialog.preview_start+dialog.preview_end)//2
        dialog.toggle_context()
        dialog.select_slot(1)
        app.processEvents()
        assert not dialog.context_rows and dialog.row == dialog.playback_row == 1
        assert not dialog.roll.changed
        dialog.toggle_context()
        dialog.context_preparer.wait_shutdown()  # App quit also cancels an active encoder.
        dialog.reject()
        app.processEvents()
        assert dialog.closed and not dialog.context_rows and not dialog.playback.want_play
    finally:
        dialog.shutdown()
        wait_until(app, lambda: dialog.context_preparer.cleaned)
        owner.close()
        app.processEvents()


@pytest.mark.parametrize("condition", ["buffering_pause", "decoder_paused", "no_frames", "seek"])
def test_music_cannot_continue_without_video(roll_movie, monkeypatch, condition):
    app = QApplication.instance() or QApplication([])
    root, _, _, _, video = roll_movie
    owner = QWidget()
    dialog = EpisodeRollDialog(root, video, owner)
    try:
        dialog.show()
        wait_loaded(dialog)
        dialog.select_slot(1)
        dialog.toggle_context()
        wait_until(app, lambda: dialog.context_ready and
                   dialog.music.playbackState() == QMediaPlayer.PlaybackState.PlayingState)
        # Keep real audio running, but reproduce readiness and decoder states that
        # used to skip the pause or leave the soundtrack playing over a frozen frame.
        if condition == "buffering_pause":
            monkeypatch.setattr(dialog.music, "mediaStatus",
                                lambda: QMediaPlayer.MediaStatus.BufferingMedia)
            dialog.playback.pause()
        elif condition == "decoder_paused":
            monkeypatch.setattr(dialog.playback.player, "playbackState",
                                lambda: QMediaPlayer.PlaybackState.PausedState)
        elif condition == "no_frames":
            dialog.playback.player.pause()
            QTest.qWait(100)  # Drain frames already queued before the simulated stall.
            # The decoder can report Playing even though no more frames arrive.
            monkeypatch.setattr(dialog.playback.player, "playbackState",
                                lambda: QMediaPlayer.PlaybackState.PlayingState)
            dialog.playback.last_frame_time = monotonic()-5
            dialog.music.play()
        else:
            dialog.playback.awaiting_seek = True
            # Keep the simulated seek pending: an already queued real video
            # frame would otherwise complete it and legitimately resume music.
            monkeypatch.setattr(dialog.playback, 'frame_ready', lambda frame: None)
        dialog.sync_music()
        assert dialog.music.playbackState() != QMediaPlayer.PlaybackState.PlayingState
        assert dialog.music_audio.volume() == 0
        stopped = dialog.music.position()
        QTest.qWait(120)
        assert abs(dialog.music.position()-stopped) < 30
        if condition == 'seek':
            assert dialog.playback.awaiting_seek
        monkeypatch.undo()
        if condition != "buffering_pause":
            dialog.playback.awaiting_seek = False
            dialog.playback.last_frame_time = monotonic()
            dialog.playback.player.play()
            wait_until(app, lambda: dialog.music.playbackState() ==
                       QMediaPlayer.PlaybackState.PlayingState)
        dialog.context_play.click()
        QTest.qWait(80)
        assert dialog.music.playbackState() != QMediaPlayer.PlaybackState.PlayingState
    finally:
        monkeypatch.undo()
        dialog.shutdown()
        wait_until(app, lambda: dialog.context_preparer.cleaned)
        owner.close()
        app.processEvents()
