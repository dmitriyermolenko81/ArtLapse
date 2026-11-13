"""Responsive scene loading and review revisions before an explicit original export."""

from pathlib import Path
from threading import Event, get_ident
from time import monotonic

import numpy as np
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QWidget
from test_episode_roll import roll_movie as roll_movie
from test_intro import make_image

from sculptcut.combined_edits import prepare_combined_edit, render_combined_edit
from sculptcut.drafts import ensure_draft, is_draft
from sculptcut.media import discover, probe, run_process
from sculptcut.output_cuts import cut_output
from sculptcut.reintro import replace_intro
from sculptcut.remusic import replace_music
from sculptcut.rendering import verify_output
from sculptcut.ui import MainWindow
from sculptcut.variants import read_variant


def wait_until(predicate, seconds=30) -> None:
    deadline = monotonic()+seconds
    while not predicate() and monotonic() < deadline:
        loop = QEventLoop()
        QTimer.singleShot(15, loop.quit)
        loop.exec()
    assert predicate()


def test_loading_is_responsive_and_can_close_before_preparation_finishes(
    roll_movie, monkeypatch,
) -> None:
    from sculptcut import episode_roll_dialog as module

    app = QApplication.instance() or QApplication([])
    root, project, plan, episodes, video = roll_movie
    original = module.load_roll_context
    entered = Event()
    stopped = Event()
    threads = []
    gui_thread = get_ident()

    def slow(root, video, cuts, cancel):
        threads.append(get_ident())
        entered.set()
        cancel.wait(0.4)
        return original(root, video, cuts, cancel)

    monkeypatch.setattr(module, "load_roll_context", slow)
    parent = QWidget()
    dialog = module.EpisodeRollDialog(root, video, parent)
    assert dialog.loading and not entered.is_set()
    dialog.show()
    dialog.loader.finished.connect(stopped.set)
    beats = []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: beats.append(monotonic()))
    timer.start()
    try:
        wait_until(entered.is_set)
        assert threads == [threads[0]] and threads[0] != gui_thread
        loop = QEventLoop()
        QTimer.singleShot(100, loop.quit)
        loop.exec()
        assert len(beats) >= 5 and dialog.isVisible()
        dialog.reject()
        assert dialog.closed and not dialog.isVisible()
        wait_until(stopped.is_set)
        assert not list((root/"previews").glob("*.mp4"))
    finally:
        timer.stop()
        dialog.shutdown()
        dialog.deleteLater()
        parent.close()
        app.processEvents()


def test_music_uses_audio_only_and_defers_cross_player_seeks(roll_movie, monkeypatch) -> None:
    from PySide6.QtMultimedia import QMediaPlayer
    from test_episode_roll import wait_loaded

    from sculptcut.episode_roll_dialog import EpisodeRollDialog

    app = QApplication.instance() or QApplication([])
    root, project, plan, episodes, video = roll_movie
    parent = QWidget()
    dialog = EpisodeRollDialog(root,video,parent)
    try:
        dialog.show()
        wait_loaded(dialog)
        dialog.music_timer.stop()
        assert Path(dialog.music.source().toLocalFile()).suffix != ".mp4"
        wait_until(lambda: dialog.music.mediaStatus() in (
            QMediaPlayer.MediaStatus.LoadedMedia,QMediaPlayer.MediaStatus.BufferedMedia))
        dialog.toggle_play()
        wait_until(lambda: dialog.playback.is_advancing() and dialog.music_should_play)
        dialog.music_timer.stop()
        calls = []
        monkeypatch.setattr(dialog.music,"setPosition",calls.append)
        dialog.position_changed(dialog.preview_start+300,force_music=True)
        assert calls == []  # No nested native decoder seek from the video's signal.
        dialog.sync_music()
        assert calls == [dialog.desired_music_ms]
    finally:
        dialog.reject()
        dialog.deleteLater()
        parent.close()
        app.processEvents()


def test_accelerated_large_source_keeps_bounded_preview_frames(tmp_path: Path) -> None:
    from sculptcut.episode_playback import EpisodePlayback

    app = QApplication.instance() or QApplication([])
    source=tmp_path/"fast.mp4"
    run_process([discover("ffmpeg"),"-v","error","-f","lavfi","-i",
        "testsrc2=s=1920x1080:r=120:d=2","-c:v","libx264","-preset","ultrafast",str(source)])
    parent=QWidget()
    playback=EpisodePlayback(parent)
    frames=[]
    beats=[]
    playback.image_ready.connect(lambda image,stamp:frames.append(image.size()))
    timer=QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda:beats.append(1))
    try:
        playback.player.setPlaybackRate(3)
        playback.select(source,0,2000,0)
        playback.play()
        timer.start()
        loop=QEventLoop()
        QTimer.singleShot(900,loop.quit)
        loop.exec()
        assert frames and len(frames)<=32
        assert all(size.width()<=960 and size.height()<=960 for size in frames)
        assert len(beats)>=10
        assert playback.thread()==app.thread()
        assert playback.sink.thread()==app.thread()
    finally:
        timer.stop()
        playback.shutdown()
        parent.close()
        app.processEvents()


def test_create_button_builds_a_draft_not_a_final_export(roll_movie, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    root, project, plan, episodes, video = roll_movie
    before = list((root/"renders").glob("*.mp4"))
    window = MainWindow()
    errors = []
    monkeypatch.setattr(window, "show_error", errors.append)
    try:
        window.folder.setText(str(root))
        window.restore_variant()
        window.preset.activate(window.preset.findData("speed"))
        window.target.setValue(0.1)
        window.output_name.setText("review-draft")
        window.show()
        app.processEvents()
        window.create_button.click()
        wait_until(lambda: bool(errors) or bool(list((root/"previews").glob("*.mp4")))
                   and window.job is None, 45)
        assert not errors
        outputs = list((root/"previews").glob("*.mp4"))
        assert len(outputs) == 1 and is_draft(outputs[0])
        assert list((root/"renders").glob("*.mp4")) == before
        state = read_variant(outputs[0])
        assert state["project"]["sources"][0]["path"] == str(project.sources[0].path)
        assert window.export_button.isEnabled()
        assert window.variants.currentData() == str(outputs[0])
    finally:
        window._allow_close = True
        window.close()
        app.processEvents()


def test_trim_music_cover_remain_drafts_then_export_the_reviewed_plan(
    roll_movie, tmp_path: Path,
) -> None:
    root, project, plan, episodes, video = roll_movie
    original = video.read_bytes()
    draft = ensure_draft(root, video, Event(), lambda _:None)
    assert is_draft(draft)
    assert probe(draft).width == project.sources[0].width
    cut = cut_output(draft, [(1_200_000,1_400_000)], Event(), lambda _:None)
    music = tmp_path/"alternate.wav"
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 "sine=frequency=660:duration=7", str(music)])
    remixed = replace_music(root, cut, music, 20, Event())
    cover = tmp_path/"other-cover.png"
    make_image(cover,(50,120,20))
    covered = replace_intro(root, remixed, cover, Event(), encoder="libx264")
    for path in (draft,cut,remixed,covered):
        assert path.parent == root/"previews" and is_draft(path)
    verify_output(covered,159,30,expected_audio=True)
    assert list((root/"renders").glob("*.mp4")) == [video]
    edit = prepare_combined_edit(root, covered, apply_current=False, exact_variant=True)
    destination = root/"renders/final-reviewed.mp4"
    render_combined_edit(root,edit,destination,Event(),lambda _:None,encoder="libx264")
    verify_output(destination,159,30,expected_audio=True)
    assert probe(destination).width == project.sources[0].width
    saved = read_variant(destination)
    assert not saved["plan"]["settings"].get("draft")
    assert video.read_bytes() == original
    # The rebuilt preview/export still fades over its own final five seconds.
    pcm = tmp_path/"final.f32"
    run_process([discover("ffmpeg"), "-v", "error", "-i", str(destination), "-vn",
                 "-ac", "1", "-ar", "48000", "-f", "f32le", str(pcm)])
    audio = np.fromfile(pcm,dtype="<f4")
    levels = [float(np.sqrt(np.mean(audio[round(t*48000):round(t*48000)+2400]**2)))
              for t in (2.1,3.1,4.1,5.2)]
    assert all(a>b for a,b in zip(levels,levels[1:],strict=False))
    assert levels[-1] < levels[0]*0.02
