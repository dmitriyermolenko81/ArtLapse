import wave
from collections.abc import Callable, Iterator
from hashlib import sha256
from pathlib import Path
from threading import Event
from time import monotonic, sleep

import numpy as np
import pytest
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from sculptcut.audio import add_music
from sculptcut.domain import Project, ProjectSettings
from sculptcut.episode_images import EpisodeImages, image_key
from sculptcut.episode_panel import EpisodePanel, episode_export
from sculptcut.idle import IdleRange
from sculptcut.media import Cancelled, discover, run_process
from sculptcut.music_library import music_signature
from sculptcut.music_waveform import build_music_wave, output_episodes
from sculptcut.planning import RenderPlan, build_plan
from sculptcut.project import create_project, load_project, save_project
from sculptcut.rendering import render, verify_output
from sculptcut.source_cards import _active_source_loaders
from sculptcut.ui import MainWindow
from sculptcut.variants import read_variant


def wait_until(app: QApplication, predicate: Callable[[], bool], seconds: float = 12) -> None:
    deadline = monotonic() + seconds
    while not predicate() and monotonic() < deadline:
        app.processEvents()
        sleep(0.01)
    assert predicate(), "GUI operation timed out"


def pulse_music(path: Path) -> Path:
    samples = np.zeros(8000, dtype=np.float32)
    for start in (1600, 5600):
        samples[start:start+400] = np.sin(np.arange(400) * 2 * np.pi * 440 / 8000) * 0.8
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes((samples * 32767).astype("<i2").tobytes())
    return path


def test_real_music_wave_loop_padding_export_clock_and_cache(
    episode_project: tuple[Path, Project, RenderPlan],
) -> None:
    root, project, plan = episode_project
    music = pulse_music(root / "pulse.wav")
    settings = ProjectSettings.model_validate(project.settings.model_dump() | {
        "music_path": music, "music_loop": False, "music_fade_seconds": 0,
    })
    result = build_music_wave(root, settings, plan, Event())
    assert result is not None and result.duration == 8
    assert len(result.levels) <= 120_000
    assert min(abs(t - 0.2) for t in result.accents) < 0.03
    assert np.max(result.levels[110:]) < 0.001  # Non-looping music is padded with silence.
    cache = next((root / "cache" / "music-wave").glob("*.npz"))
    modified = cache.stat().st_mtime_ns
    cached = build_music_wave(root, settings, plan, Event())
    assert cached is not None and np.array_equal(cached.levels, result.levels)
    assert cache.stat().st_mtime_ns == modified
    loop_settings = settings.model_copy(update={"music_loop": True})
    looped = build_music_wave(root, loop_settings, plan, Event())
    assert looped is not None and min(abs(t - 7.2) for t in looped.accents) < 0.03
    output = root / "with-pulses.mp4"
    add_music(project.sources[0].path, music, output, 8, True, 0)
    verify_output(output, 240, 30, expected_audio=True)
    pcm = root / "actual.f32"
    run_process([discover("ffmpeg"), "-v", "error", "-i", str(output), "-vn",
                 "-ac", "1", "-ar", "4000", "-f", "f32le", str(pcm)])
    samples = np.fromfile(pcm, dtype="<f4")
    peak_time = (np.argmax(np.abs(samples[7*4000:])) / 4000) + 7
    assert 7.19 < peak_time < 7.76
    assert min(abs(t - peak_time) for t in looped.accents) < 0.06
    # Output positions include the intro overlap and internal montage-frame changes.
    intro = settings.model_copy(update={"intro_image": root / "cover.png",
                                       "intro_seconds": 2, "intro_transition_seconds": 0.5})
    mapping = output_episodes(intro, plan)
    assert mapping[0].start == 1.5 and mapping[1].start == 3.5 and mapping[1].row == 2
    montage = plan.decisions[0].model_copy(update={"action": "MONTAGE",
                                                 "sample_times_us": [0, 1_000_000]})
    assert [e.start for e in output_episodes(intro, plan.model_copy(
        update={"decisions": [montage, *plan.decisions[1:]]}))][:3] == [1.5, 2.5, 3.5]
    cancelled = Event()
    cancelled.set()
    with pytest.raises(Cancelled):
        build_music_wave(root, settings, plan, cancelled)


def test_music_wave_click_selects_source_and_refreshes_cuts_after_edit(
    episode_project: tuple[Path, Project, RenderPlan],
) -> None:
    root, project, plan = episode_project
    settings = ProjectSettings.model_validate(project.settings.model_dump() | {
        "music_path": pulse_music(root / "pulses.wav"), "music_fade_seconds": 0,
    })
    save_project(root, project.model_copy(update={"settings": settings}))
    plan = plan.model_copy(update={"settings": plan.settings | {
        "music_fingerprint": music_signature(settings),
    }})
    (root / "render_plan.json").write_text(plan.model_dump_json(), encoding="utf-8")
    app = QApplication.instance() or QApplication([])
    owner = MainWindow()
    panel = EpisodePanel(root, owner)
    owner.episode_panel = panel
    panel.editor.edit_saved.connect(lambda old, new, label:
                                    owner.history_controller.record_episode(root, old, new, label))
    before = (root / "project.json").read_bytes()
    try:
        panel.show()
        assert panel.music_button.isHidden() and panel.music.isHidden()
        assert panel.music.worker is None  # Work starts only when requested.
        panel.music_button.click()
        wait_until(app, lambda: panel.music.wave is not None)
        assert [e.start for e in panel.music.episodes] == [0, 2, 4, 6]
        panel.show_kept.setChecked(False)
        panel.music.set_view(0, 8)
        QTest.mouseClick(panel.music, Qt.MouseButton.LeftButton,
                         pos=QPoint(round(panel.music.x_at(4)), 45))
        assert panel.sources.currentIndex() == 1 and panel.timeline.selected == 3
        assert panel.show_kept.isChecked()
        assert panel.music.selected == 3
        assert (root / "project.json").read_bytes() == before
        panel.editor.apply_choice(panel.editor.item(3), "slow")
        assert panel.music.episodes[2].end > 6  # Fixed slow shot changes neighbouring cut times.
        owner.history_controller.undo()
        assert [e.start for e in panel.music.episodes] == [0, 2, 4, 6]
        owner.history_controller.redo()
        assert panel.music.episodes[2].end > 6
        panel.music.set_plan(settings, plan.model_copy(update={"actual_frame_count": 300}))
        assert panel.music.worker is not None and panel.music.worker.isRunning()
        panel.reject()
        assert panel.music.closed
        assert panel.music.worker is None or not panel.music.worker.isRunning()
    finally:
        panel.reject()
        owner.episode_panel = None
        owner.close()
        app.processEvents()


@pytest.fixture
def episode_project(tmp_path: Path) -> tuple[Path, Project, RenderPlan]:
    first, second = tmp_path / "красный и синий.mp4", tmp_path / "зелёный.mp4"
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 "color=c=red:s=320x180:r=30:d=4", "-f", "lavfi", "-i",
                 "color=c=blue:s=320x180:r=30:d=4", "-filter_complex",
                 "[0:v][1:v]concat=n=2:v=1:a=0[out]", "-map", "[out]",
                 "-c:v", "libx264", str(first)])
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 "color=c=green:s=320x180:r=30:d=8", "-c:v", "libx264", str(second)])
    root = tmp_path / "project"
    project = create_project(root, [first, second])
    project = project.model_copy(update={"settings": ProjectSettings(
        target_seconds=8, final_view_seconds=0, output_name="модель",
    )})
    save_project(root, project)
    plan = build_plan(project, [IdleRange(source_id=s.id, start_us=3_000_000,
                                        end_us=5_000_000) for s in project.sources], 8)
    plan = plan.model_copy(update={"settings": plan.settings | {"width": 320, "height": 180}})
    (root / "render_plan.json").write_text(plan.model_dump_json(), encoding="utf-8")
    return root, project, plan


@pytest.fixture
def visual_panel(episode_project: tuple[Path, Project, RenderPlan]) -> Iterator[
    tuple[QApplication, QWidget, EpisodePanel]
]:
    app = QApplication.instance() or QApplication([])
    parent = QWidget()
    panel = EpisodePanel(episode_project[0], parent)
    panel.show()
    app.processEvents()
    try:
        yield app, parent, panel
    finally:
        panel.reject()
        parent.close()
        app.processEvents()


def test_real_visible_card_images_source_switch_and_frame_position(
    visual_panel: tuple[QApplication, QWidget, EpisodePanel],
) -> None:
    app, _, panel = visual_panel
    wait_until(app, lambda: not panel.editor.item(0).icon().isNull())
    wait_until(app, lambda: all(not panel.editor.item(i).icon().isNull() for i in range(3)))
    assert panel.editor.isRowHidden(3)
    panel.request_frame(6000)
    # Opening already supplies a real frame; wait for this later seek to finish.
    wait_until(app, lambda: panel.frames.worker is None and not panel.frames.pending)
    color = panel.picture.pixmap().toImage().pixelColor(10, 10)
    assert color.blue() > 200 and color.red() < 20
    assert "00:06.00" in panel.time_label.text()
    panel.sources.setCurrentIndex(1)
    assert panel.editor.isRowHidden(0) and not panel.editor.isRowHidden(3)
    panel.request_frame(1000)
    wait_until(app, lambda: panel.picture.pixmap() is not None
               and not panel.picture.pixmap().isNull())
    color = panel.picture.pixmap().toImage().pixelColor(10, 10)
    assert color.green() > 100 and color.blue() < 20
    assert len(panel.cards.cache) <= 48 and len(panel.frames.cache) <= 2


def test_real_episode_playback_scrub_stop_loop_source_switch_and_close(
    visual_panel: tuple[QApplication, QWidget, EpisodePanel],
) -> None:
    app, _, panel = visual_panel
    panel.editor.preview_episode(panel.editor.item(0))
    playback = panel.playback
    received: list[tuple[QImage, int]] = []
    playback.image_ready.connect(lambda image, position: received.append((image, position)))
    wait_until(app, lambda: playback.pending is None)
    assert playback.start == 0 and playback.end == 3000
    # Actual decoder playback must advance, deliver video, then stop at the cut.
    panel.seek_playback(2600)
    panel.play_button.click()
    wait_until(app, lambda: bool(received) and playback.player.position() > 2700)
    wait_until(app, lambda: not playback.want_play)
    assert playback.player.playbackState() == QMediaPlayer.PlaybackState.PausedState
    assert playback.player.position() == 2999
    assert received[-1][0].pixelColor(10, 10).red() > 200
    # Loop restarts the selected interval rather than continuing into the next.
    panel.loop_button.setChecked(True)
    panel.seek_playback(2800)
    panel.play_button.click()
    wait_until(app, lambda: playback.player.position() < 1000 and playback.want_play)
    panel.play_button.click()
    assert not playback.want_play
    # A real mouse scrub seeks through the source without changing trim/overrides.
    before = load_project(panel.root).overrides
    timeline = panel.timeline
    timeline.set_view(0, timeline.duration)
    def point(us: int) -> QPoint:
        return QPoint(round(timeline.x_at(us)), 15)
    QTest.mousePress(timeline, Qt.MouseButton.LeftButton, pos=point(1_000_000))
    QTest.mouseMove(timeline, point(6_000_000))
    QTest.mouseRelease(timeline, Qt.MouseButton.LeftButton, pos=point(6_000_000))
    wait_until(app, lambda: received[-1][1] >= 5900
               and received[-1][0].pixelColor(10, 10).blue() > 200)
    assert not playback.want_play and load_project(panel.root).overrides == before
    assert timeline.selected_decision().source_start_us == 0
    panel.sources.setCurrentIndex(1)
    assert not panel.play_button.isEnabled() and not playback.want_play
    panel.editor.preview_episode(panel.editor.item(3))
    panel.play_button.click()  # Also exercises play while the new source is loading.
    wait_until(app, lambda: bool(received) and received[-1][0].pixelColor(10, 10).green() > 100)
    assert playback.want_play
    panel.reject()
    assert playback.closed and not playback.timer.isActive()
    assert playback.player.source().isEmpty()
    assert playback.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState


def test_episode_filters_preserve_edit_and_refresh_statistics_after_edit_and_undo(
    episode_project: tuple[Path, Project, RenderPlan],
) -> None:
    app = QApplication.instance() or QApplication([])
    root, _, _ = episode_project
    before = (root / "project.json").read_bytes()
    base = (root / "render_plan.json").read_bytes()
    export_before = episode_export(root)[1]
    window = MainWindow()
    panel = EpisodePanel(root, window)
    window.episode_panel = panel
    panel.bind_history(window.history_controller.undo_action, window.history_controller.redo_action)
    panel.editor.edit_saved.connect(lambda old, new, label:
                                    window.history_controller.record_episode(root, old, new, label))
    panel.show()
    def visible() -> list[int]:
        return [i for i in range(panel.editor.count()) if not panel.editor.isRowHidden(i)]
    try:
        assert "Всего: 6 эп. · 00:16.0" in panel.summary.toolTip()
        assert "В ролике: 4 эп. · 00:12.0" in panel.summary.toolTip()
        assert "00:08.0 в готовом ролике" in panel.summary.toolTip()
        assert "Пропуск: 2 эп. · 00:04.0" in panel.summary.toolTip()
        panel.show_dropped.setChecked(False)
        assert visible() == [0, 2]
        assert [i for i, _ in panel.timeline.visible_decisions()] == [0, 2]
        panel.editor.preview_episode(panel.editor.item(0))
        panel.play_button.click()
        wait_until(app, lambda: panel.playback.player.playbackState()
                   == QMediaPlayer.PlaybackState.PlayingState)
        panel.show_kept.setChecked(False)
        assert not visible() and not panel.timeline.visible_decisions()
        assert not panel.playback.want_play and not panel.play_button.isEnabled()
        assert panel.timeline.selected_decision() is None
        panel.show_dropped.setChecked(True)
        assert visible() == [1]
        panel.timeline.set_view(0, panel.timeline.duration)
        # Hidden kept bands must not intercept a click on the timeline.
        QTest.mouseClick(panel.timeline, Qt.MouseButton.LeftButton,
                         pos=QPoint(round(panel.timeline.x_at(1_000_000)), 15))
        assert panel.timeline.selected_decision() is None
        panel.sources.setCurrentIndex(1)
        assert visible() == [4]
        assert "Показано: 1 из 3" in panel.shown_label.toolTip()
        assert (root / "project.json").read_bytes() == before
        assert (root / "render_plan.json").read_bytes() == base
        assert episode_export(root)[1] == export_before
        assert window.edit_history.position == 0
        # A real edit removes the now-kept episode from the active drop filter.
        panel.editor.apply_choice(panel.editor.item(4), "keep")
        assert not visible()
        assert "В ролике: 5 эп. · 00:14.0" in panel.summary.toolTip()
        assert "Пропуск: 1 эп. · 00:02.0" in panel.summary.toolTip()
        # The planner redistributes other speeds to preserve the 8-second target.
        assert "00:08.0 в готовом ролике" in panel.summary.toolTip()
        panel.undo_button.click()
        assert visible() == [4]
        assert "В ролике: 4 эп. · 00:12.0" in panel.summary.toolTip()
        assert "Пропуск: 2 эп. · 00:04.0" in panel.summary.toolTip()
        panel.redo_button.click()
        assert not visible() and "Пропуск: 1 эп." in panel.summary.toolTip()
        panel.show_kept.setChecked(True)
        assert len(visible()) == 3
    finally:
        panel.reject()
        window.episode_panel = None
        window.close()
        app.processEvents()


def test_previous_next_follow_visible_source_episodes_and_pause_playback(
    visual_panel: tuple[QApplication, QWidget, EpisodePanel],
) -> None:
    app, _, panel = visual_panel
    before = (panel.root / "project.json").read_bytes()
    panel.show_dropped.setChecked(False)
    assert panel.timeline.selected == panel.editor.currentRow() == 0
    assert not panel.previous_button.isEnabled()
    panel.play_button.click()
    wait_until(app, lambda: panel.playback.player.playbackState()
               == QMediaPlayer.PlaybackState.PlayingState)
    panel.next_button.click()
    assert panel.timeline.selected == panel.editor.currentRow() == 2
    assert not panel.playback.want_play and panel.playback.start == 5000
    assert not panel.next_button.isEnabled()
    panel.navigate_episode(1)
    assert panel.timeline.selected == 2  # No wrap past the last visible episode.
    panel.previous_button.click()
    assert panel.timeline.selected == 0
    panel.show_kept.setChecked(False)
    assert not panel.next_button.isEnabled() and not panel.previous_button.isEnabled()
    panel.show_dropped.setChecked(True)
    panel.next_button.click()
    assert panel.timeline.selected == panel.editor.currentRow() == 1
    assert not panel.next_button.isEnabled() and not panel.previous_button.isEnabled()
    panel.sources.setCurrentIndex(1)
    panel.next_button.click()
    assert panel.timeline.selected == panel.editor.currentRow() == 4
    panel.show_kept.setChecked(True)
    panel.next_button.click()
    assert panel.timeline.selected == 5
    panel.previous_button.click()
    panel.previous_button.click()
    assert panel.timeline.selected == 3
    assert not panel.previous_button.isEnabled()
    assert (panel.root / "project.json").read_bytes() == before


def test_drag_boundary_has_real_frame_commits_once_and_noop_does_not_save(
    visual_panel: tuple[QApplication, QWidget, EpisodePanel],
    episode_project: tuple[Path, Project, RenderPlan],
) -> None:
    app, _, panel = visual_panel
    timeline = panel.timeline
    panel.editor.preview_episode(panel.editor.item(0))
    panel.play_button.click()
    wait_until(app, lambda: panel.playback.player.playbackState()
               == QMediaPlayer.PlaybackState.PlayingState)
    timeline.set_position(1000)
    saved: list[str] = []
    panel.editor.edit_saved.connect(lambda before, after, label: saved.append(label))
    def point(us: int) -> QPoint:
        return QPoint(round(timeline.x_at(us)), 15)
    QTest.mousePress(timeline, Qt.MouseButton.LeftButton, pos=point(3_000_000))
    assert panel.cards.paused
    assert not panel.playback.want_play
    for us in (2_800_000, 2_500_000, 2_000_000):
        QTest.mouseMove(timeline, point(us))
    assert panel.popup.isVisible()
    wait_until(app, lambda: panel.popup_image.pixmap() is not None
               and not panel.popup_image.pixmap().isNull())
    color = panel.popup_image.pixmap().toImage().pixelColor(10, 10)
    assert color.red() > 200 and color.blue() < 20
    QTest.mouseRelease(timeline, Qt.MouseButton.LeftButton, pos=point(2_000_000))
    assert not panel.cards.paused and not panel.popup.isVisible()
    assert saved == ["Границы эпизода"]
    assert panel.message.text() == "Правка сохранена."
    project, plan = episode_export(episode_project[0])
    kept = [d for d in plan.decisions
            if d.source_id == project.sources[0].id and d.action != "DROP"]
    assert abs(kept[0].source_end_us - 2_000_000) < 20_000
    wait_until(app, lambda: panel.play_button.isEnabled() and panel.playback.end < 2020)
    timeline.set_position(1000)
    end = panel.timeline.selected_decision().source_end_us
    QTest.mousePress(timeline, Qt.MouseButton.LeftButton, pos=point(end))
    QTest.mouseRelease(timeline, Qt.MouseButton.LeftButton, pos=point(end))
    assert saved == ["Границы эпизода"]


def test_latest_requests_reject_another_source_and_shutdown_stops_queue(
    episode_project: tuple[Path, Project, RenderPlan],
) -> None:
    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    loader = EpisodeImages(owner, limit=2, intermediate=True)
    project = episode_project[1]
    keys = [image_key(project.sources[0].path, 1_000_000),
            image_key(project.sources[0].path, 6_000_000),
            image_key(project.sources[1].path, 1_000_000)]
    received: list[tuple[object, QImage]] = []
    loader.ready.connect(lambda key, image: received.append((key, image)))
    try:
        loader.request(keys[:1])
        wait_until(app, lambda: loader.worker is not None)
        loader.request(keys[-1:])
        wait_until(app, lambda: bool(received) and received[-1][0] == keys[-1])
        assert all(key == keys[-1] for key, _ in received)
        loader.request([keys[1]])
        wait_until(app, lambda: received[-1][0] == keys[1])
        assert len(loader.cache) == 2
        loader.request(keys)
        loader.shutdown()
        assert not loader.pending and not loader.cache
        count = len(received)
        app.processEvents()
        assert len(received) == count
    finally:
        loader.shutdown()
        owner.close()


def test_episode_edit_joins_global_history_and_survives_reopen_and_real_export(
    episode_project: tuple[Path, Project, RenderPlan],
) -> None:
    app = QApplication.instance() or QApplication([])
    root, project, base = episode_project
    hashes = {s.path: sha256(s.path.read_bytes()).digest() for s in project.sources}
    base_bytes = (root / "render_plan.json").read_bytes()
    window = MainWindow()
    window.open_project_folder(root)
    window.show()
    errors: list[str] = []

    def edit_panel() -> None:
        panel = window.episode_panel
        if panel is None or not panel.isVisible():
            QTimer.singleShot(20, edit_panel)
            return
        try:
            panel.timeline.set_position(1000)
            a = QPoint(round(panel.timeline.x_at(3_000_000)), 15)
            b = QPoint(round(panel.timeline.x_at(2_000_000)), 15)
            QTest.mousePress(panel.timeline, Qt.MouseButton.LeftButton, pos=a)
            QTest.mouseMove(panel.timeline, b)
            QTest.mouseRelease(panel.timeline, Qt.MouseButton.LeftButton, pos=b)
            assert window.edit_history.position == 2
            panel.undo_button.click()
            assert not load_project(root).overrides
            panel.redo_button.click()
            assert load_project(root).overrides
            panel.activateWindow()
            app.processEvents()
            QTest.keyClick(panel, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
            assert not load_project(root).overrides
            QTest.keyClick(panel, Qt.Key.Key_Z,
                           Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
            assert load_project(root).overrides
        except AssertionError as exc:
            errors.append(repr(exc))
        panel.reject()

    try:
        window.duration_card.minutes.setValue(1)
        window.duration_card.minutes.editingFinished.emit()
        QTimer.singleShot(20, edit_panel)
        window.episodes_button.click()
        assert not errors
        assert window.edit_history.position == 2
        edited = load_project(root).overrides
        window.history_controller.undo()
        assert not load_project(root).overrides
        window.history_controller.undo()
        assert window.target.value() == 8 / 60 or abs(window.target.value() - 8 / 60) < 1e-6
        window.history_controller.redo()
        window.history_controller.redo()
        assert load_project(root).overrides == edited
        reopened = EpisodePanel(root, window)
        assert reopened.editor.count() >= 3
        reopened.reject()
        window.render_episodes(root)
        wait_until(app, lambda: window.job is None, 60)
        assert not window.last_error, window.last_error
        outputs = list((root / "renders").glob("*.mp4"))
        assert len(outputs) == 1 and "исправленный" in outputs[0].name
        _, revised = episode_export(root)
        verify_output(outputs[0], revised.actual_frame_count, revised.output_fps)
        variant = read_variant(outputs[0])
        assert variant["project"]["overrides"]
        assert variant["plan"]["decisions"] != base.model_dump()["decisions"]
        assert (root / "render_plan.json").read_bytes() == base_bytes
        assert all(sha256(path.read_bytes()).digest() == value for path, value in hashes.items())
        # A different automatic plan must not receive overrides from the old history.
        position = window.edit_history.position
        (root / "render_plan.json").write_text(base.model_dump_json(indent=2), encoding="utf-8")
        window.history_controller.undo()
        assert window.edit_history.position == position
        assert load_project(root).overrides == edited
        assert "Монтаж изменился" in window.last_error
    finally:
        if window.job is not None:
            window.job.cancel.set()
            wait_until(app, lambda: window.job is None, 60)
        wait_until(app, lambda: not _active_source_loaders)
        window.close()
        app.processEvents()


def test_fixed_still_boundaries(
    visual_panel: tuple[QApplication, QWidget, EpisodePanel],
    episode_project: tuple[Path, Project, RenderPlan],
) -> None:
    _, _, panel = visual_panel
    decision = panel.editor.item(0).data(Qt.ItemDataRole.UserRole)
    panel.editor.item(0).setData(Qt.ItemDataRole.UserRole, decision.model_copy(update={
        "action": "HOLD", "provenance": "user:final_tail",
    }))
    panel.timeline.set_position(1000)
    assert panel.timeline.handle_at(panel.timeline.x_at(3_000_000)) == ""


def test_missing_and_invalid_saved_plan_do_not_open_editor(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.folder.setText(str(tmp_path))
        assert not window.episodes_button.isEnabled()
        window.show_episodes()
        assert window.last_error and window.episode_panel is None
    finally:
        window.close()
        app.processEvents()


def test_long_ribbon_decodes_visible_cards_only(
    episode_project: tuple[Path, Project, RenderPlan],
) -> None:
    app = QApplication.instance() or QApplication([])
    root, project, plan = episode_project
    first = plan.decisions[0]
    decisions = [first.model_copy(update={
        "source_start_us": i * 200_000, "source_end_us": (i + 1) * 200_000,
        "output_frame_count": 3,
    }) for i in range(40)]
    decisions += [d for d in plan.decisions if d.source_id != project.sources[0].id]
    plan = plan.model_copy(update={"decisions": decisions})
    (root / "render_plan.json").write_text(plan.model_dump_json(), encoding="utf-8")
    owner = QWidget()
    panel = EpisodePanel(root, owner)
    panel.resize(800, 650)
    panel.show()
    try:
        wait_until(app, lambda: bool(panel.cards.cache) and panel.cards.worker is None
                   and not panel.cards.pending)
        count = len(panel.cards.cache)
        assert 1 <= count < 10  # Forty episodes exist; only the viewport is decoded.
        assert panel.editor.item(30).icon().isNull()
        bar = panel.editor.horizontalScrollBar()
        assert bar.maximum() > 0
        bar.setValue(bar.maximum())
        wait_until(app, lambda: not panel.editor.item(39).icon().isNull())
        assert len(panel.cards.cache) < 20
        assert panel.editor.item(0).icon().isNull()
    finally:
        panel.reject()
        owner.close()
        app.processEvents()


def test_saved_music_settings_survive_next_seed_and_corrected_export(
    episode_project: tuple[Path, Project, RenderPlan], tmp_path: Path,
) -> None:
    app = QApplication.instance() or QApplication([])
    root, project, plan = episode_project
    music = tmp_path / "музыка"
    music.mkdir()
    for index in range(2):
        run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                     f"sine=frequency={440 + 220 * index}:duration=2", str(music / f"{index}.wav")])
    # Use the same validation as MainWindow before planning (normalize numeric defaults).
    settings = ProjectSettings.model_validate(
        project.settings.model_dump() | {"music_folder": music, "music_seed": 7}
    )
    project = project.model_copy(update={"settings": settings})
    save_project(root, project)
    plan = build_plan(project, [IdleRange(source_id=s.id, start_us=3_000_000,
                                        end_us=5_000_000) for s in project.sources], 8)
    plan = plan.model_copy(update={"settings": plan.settings | {"width": 320, "height": 180}})
    (root / "render_plan.json").write_text(plan.model_dump_json(), encoding="utf-8")
    original = root / "renders" / "original.mp4"
    render(root, project, plan, original, encoder="libx264")
    original_hash = sha256(original.read_bytes()).digest()
    # This is what MainWindow.show_result does after a normal music export.
    next_settings = settings.model_copy(update={"music_seed": 999})
    save_project(root, project.model_copy(update={"settings": next_settings}))
    loaded, _ = episode_export(root)
    assert loaded.settings.music_seed == 7
    owner = QWidget()
    panel = EpisodePanel(root, owner)
    failures: list[str] = []
    panel.editor.failed.connect(failures.append)
    try:
        assert panel.editor.count() >= 3
        panel.music_button.click()
        wait_until(app, lambda: panel.music.wave is not None)
        assert panel.project.settings.music_seed == 7
        assert load_project(root).settings.music_seed == 999
        panel.editor.apply_choice(panel.editor.item(0), "slow")
        assert not failures
        assert load_project(root).settings.music_seed == 999
        edited, corrected = episode_export(root)
        output = root / "renders" / "corrected.mp4"
        render(root, edited, corrected, output, encoder="libx264")
        verify_output(output, corrected.actual_frame_count, corrected.output_fps,
                      expected_audio=True)
        assert read_variant(output)["project"]["settings"]["music_seed"] == 7
        assert sha256(original.read_bytes()).digest() == original_hash
        assert load_project(root).settings.music_seed == 999
    finally:
        panel.reject()
        owner.close()
        app.processEvents()
