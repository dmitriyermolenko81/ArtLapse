"""Audio follower ignores decoder clock jitter and waits for real video frames."""
from types import SimpleNamespace

from PySide6.QtMultimedia import QMediaPlayer
from test_music_edit import music_movie as music_movie

from sculptcut.inline_music import InlineMusic


class Audio:
    def __init__(self):
        self.ms = 880
        self.state = QMediaPlayer.PlaybackState.PlayingState
        self.seeks = []
    def position(self): return self.ms
    def playbackState(self): return self.state
    def isSeekable(self): return True
    def setPosition(self, ms):
        self.seeks.append(ms)
        self.ms = ms
    def pause(self): self.state = QMediaPlayer.PlaybackState.PausedState
    def play(self): self.state = QMediaPlayer.PlaybackState.PlayingState

def follower():
    clock = SimpleNamespace(ms=1000, advancing=True)
    audio = Audio()
    owner = SimpleNamespace(closed=False, montage_seek=SimpleNamespace(value=lambda: clock.ms),
        sequence=SimpleNamespace(enabled=True, desired=True, waiting=False),
        playback=SimpleNamespace(is_advancing=lambda: clock.advancing))
    return clock, audio, SimpleNamespace(owner=owner, player=audio, independent=True,
                                        audition_ready=True, pending_seek_ms=None)

def test_continuous_audio_does_not_seek_for_coarse_position_notifications():
    clock, audio, track = follower()
    for ms in range(1000, 3000, 40):
        clock.ms, audio.ms = ms, ms-120
        InlineMusic.sync_audio(track)
    assert audio.seeks == []
    clock.ms = 5000
    InlineMusic.sync_audio(track)
    assert audio.seeks == [5000]

def test_stalled_video_pauses_music_without_repeated_seeks_and_resumes_at_clock():
    clock, audio, track = follower()
    clock.advancing = False
    for _ in range(20):
        InlineMusic.sync_audio(track)
    assert audio.state == QMediaPlayer.PlaybackState.PausedState and not audio.seeks
    clock.ms, clock.advancing = 4000, True
    InlineMusic.sync_audio(track)
    assert audio.seeks == [4000]
    assert audio.state == QMediaPlayer.PlaybackState.PlayingState

def test_real_music_playback_does_not_reseek_during_continuous_video(monkeypatch, music_movie):
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from test_cursor_playback import finish, open_editor
    from test_episode_panel import wait_until

    root, video, clips = music_movie
    app = QApplication.instance() or QApplication([])
    window, source = open_editor(root, video, app)
    music = source.music_track
    assert music is not None
    try:
        music.commit(clips)
        source.seek_montage(200)
        source.play_montage()
        wait_until(app, lambda: music.worker is None and source.playback.is_advancing()
            and music.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState, 25)
        seeks = []
        original = music.player.setPosition
        def record(ms):
            seeks.append(ms)
            original(ms)
        monkeypatch.setattr(music.player, 'setPosition', record)
        QTest.qWait(1200)
        assert len(seeks) <= 2, seeks
        assert source.playback.audio.isMuted() and not music.audio.isMuted()
        source.playback.pause()
        QTest.qWait(100)
        assert music.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState
    finally:
        finish(window, source, app)
