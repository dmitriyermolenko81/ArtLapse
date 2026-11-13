"""Prepared decoder handoff retains public signals, volume and cancellation."""
from PySide6.QtWidgets import QApplication, QWidget
from test_episode_panel import wait_until
from test_episode_roll import roll_movie as roll_movie

from sculptcut.episode_playback import EpisodePlayback


def test_adopt_decoded_backend_without_reopening_and_keep_public_signals(roll_movie):
    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    active, standby = EpisodePlayback(owner), EpisodePlayback(owner)
    frames = []
    try:
        active.audio.setVolume(0.3)
        active.audio.setMuted(False)
        active.image_ready.connect(lambda image, ms: frames.append(ms))
        standby.audio.setMuted(True)
        standby.select(roll_movie[-1], 0, 5000, 0)
        standby.play()
        wait_until(app, standby.is_advancing)
        standby.pause()
        standby.seek(0)
        wait_until(app, lambda: not standby.awaiting_seek)
        backend = standby.player
        sources = []
        backend.sourceChanged.connect(sources.append)
        active.take_preloaded(standby)
        assert active.player is backend
        assert frames  # First picture is available at handoff, before resuming.
        assert not active.audio.isMuted() and abs(active.audio.volume()-0.3) < 0.01
        assert standby.audio.isMuted() and not standby.want_play
        active.play()
        wait_until(app, lambda: bool(frames) and active.is_advancing())
        assert not sources
        active.pause()
        assert not active.want_play
    finally:
        active.shutdown()
        standby.shutdown()
        owner.close()
        app.processEvents()
