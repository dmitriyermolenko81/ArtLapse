import time
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent, QPixmap
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QLabel, QMessageBox

from sculptcut.domain import Override, ProjectSettings
from sculptcut.intro import output_frame_count
from sculptcut.media import discover, run_process
from sculptcut.project import create_project, load_project, save_project
from sculptcut.rendering import verify_output
from sculptcut.ui import Job, MainWindow, format_hms


def test_long_titles_and_errors_keep_compact_window() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.resize(1120, 700)
    window.show()
    message = "MediaError: " + "Длинное имя исходного файла_" * 60
    title = "Очень длинное имя готового ролика_" * 40 + ".mp4"
    try:
        window.show_error(message)
        window.video_title.setText(title)
        QTest.qWait(100)
        assert window.width() == 1120
        assert window.status.text() != QLabel.text(window.status)
        assert window.status.toolTip() == message
        assert window.video_title.text() == title
        assert window.video_title.toolTip() == title
        assert window.settings_scroll.verticalScrollBar().maximum() == 0
    finally:
        window.close()
        app.processEvents()


def test_error_diagnostics_survive_progress_and_can_be_copied() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    message = "MediaError: Cannot read selected timelapse footage: example.mkv"
    previous_clipboard = app.clipboard().text()
    try:
        window.job_started_at = time.monotonic() - 5
        window.show_error(message)
        window.update_progress_details()
        assert window.status.toolTip() == message

        def copy_from_dialog() -> None:
            dialog = window.findChildren(QMessageBox)[-1]
            button = next(b for b in dialog.buttons() if b.text() == "Копировать ошибку")
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            dialog.accept()

        QTimer.singleShot(50, copy_from_dialog)
        window.show_error_details()
        assert app.clipboard().text() == message
    finally:
        app.clipboard().setText(previous_clipboard)
        window.job_started_at = None
        window.close()
        app.processEvents()


def test_job_limits_progress_signals_for_long_analysis() -> None:
    job = Job(lambda cancel, progress, stage: "")
    events = QSignalSpy(job.progress)
    for index in range(1000):
        job.report_progress(index / 1000)
    assert events.count() == 1
    job.report_progress(-1)
    job.report_progress(1)
    assert events.count() == 3


def test_shorts_use_create_button_without_preview_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    try:
        app.processEvents()
        assert not hasattr(window, "preview_button")
        window.preset.setCurrentIndex(window.preset.findData("shorts"))
        app.processEvents()
        assert window.create_button.isVisible()
        assert window.create_button.text() == "Создать ролик"
        assert not hasattr(window, "beat_sync")
        assert not hasattr(window, "beat_sync_mode")
        assert not hasattr(window, "beat_sync_strength")
        assert not hasattr(window, "beat_sync")
        window.preset.setCurrentIndex(window.preset.findData("beat_shorts"))
        app.processEvents()
        assert not hasattr(window, "beat_sync")
        from PySide6.QtWidgets import QLabel

        assert not {"Ритм", "Сила синхронизации"} & {
            label.text() for label in window.findChildren(QLabel)
        }
        window.preset.setCurrentIndex(window.preset.findData("shorts"))
        app.processEvents()
        assert not hasattr(window, "beat_sync")
        assert not hasattr(window, "render_action")
        actions: list[str] = []
        monkeypatch.setattr(window, "start_action", actions.append)
        window.create_button.click()
        assert actions == ["create"]
        window.preset.setCurrentIndex(window.preset.findData("timelapse"))
        app.processEvents()
        assert window.create_button.isVisible()
    finally:
        window.close()
        app.processEvents()


def test_player_clock_uses_hours_minutes_seconds() -> None:
    assert format_hms(6331) == "01:45:31"
    assert format_hms(6450) == "01:47:30"


@pytest.mark.parametrize("with_music", [False, True])
def test_gui_real_pipeline_and_responsiveness(
    tmp_path: Path, with_music: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    app.processEvents()
    window.create_button.click()
    assert "Choose a project" in window.last_error
    source = tmp_path / "source.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=160x120:r=15:d=12",
            "-c:v",
            "libx264",
            str(source),
        ]
    )
    root = tmp_path / "gui project"
    existing = create_project(root, [source]).model_copy(
        update={"settings": ProjectSettings(style="calm", music_loop=False,
                                            beat_sync_mode="slow", beat_sync_strength=0.25)}
    )
    existing = existing.model_copy(
        update={
            "overrides": [
                Override(
                    source_id="source-0000", start_us=1_000_000, end_us=2_000_000, action="KEEP"
                )
            ]
        }
    )
    save_project(root, existing)
    window.folder.setText(str(root))
    window.set_sources([source])
    assert window.player.source().isEmpty()
    assert "готовый ролик" in window.video.text()
    assert window.source_stack.currentWidget() is window.sources
    assert window.seek.maximum() == 0
    window.target.setValue(0.1)  # Minutes in the UI, six seconds in persisted settings.
    intro_image = None
    if not with_music:
        window.output_name.setText("Моя модель.mp4")
        intro_image = tmp_path / "model intro.png"
        image = QPixmap(64, 48)
        image.fill(Qt.GlobalColor.green)
        assert image.save(str(intro_image))
        window.intro_image.setText(str(intro_image))
    if with_music:
        folder = tmp_path / "music"
        folder.mkdir()
        for index in range(2):
            run_process(
                [
                    discover("ffmpeg"),
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    f"sine=frequency={440 + index * 440}:duration=0.7",
                    str(folder / f"track{index}.wav"),
                ]
            )
        window.music.setText(str(folder))
        window.music_selection.setCurrentIndex(window.music_selection.findData("full_tracks"))
        window.shuffle_music()
    ticks: list[int] = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(10)
    try:
        stages: list[str] = []
        window.job_stage.connect(stages.append)
        actions = ("render", "preview") if with_music else ("create",)
        for action in actions:
            assert window.create_button.isVisible()
            assert not hasattr(window, "preview_button")
            assert window.change_music_button.isVisible()
            assert window.change_intro_button.isVisible()
            assert window.outputs_button.isVisible()
            if action == "create":
                window.create_button.click()
            else:
                window.start_action(action)
            assert window.job is not None
            assert window.progress_bar.maximum() == 0
            deadline = time.monotonic() + 60
            while window.job is not None and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)  # Release the GIL while the real worker analyzes frames.
            assert window.job is None, "GUI job timed out"
            assert not window.last_error, window.last_error
            if not with_music:
                assert (root / "renders" / "Моя модель_timelapse.mp4").is_file()
                assert window.create_button.text() == "Создать ролик"
                assert not hasattr(window, "review_panel")
                assert not hasattr(window, "episode_timeline")
        assert len(ticks) > 10
        saved = load_project(root)
        assert saved.settings.beat_sync_mode == "fast"
        assert saved.settings.beat_sync_strength == 1.0
        # Manual episode choices persist across modes and subsequent exports.
        assert saved.overrides == existing.overrides
        assert saved.settings.style == "calm"
        assert saved.settings.music_loop is False
        assert saved.settings.target_seconds == 6
        assert saved.settings.intro_image == intro_image
        assert saved.settings.output_name == ("Моя модель" if not with_music else "")
        assert not (root / "review.html").exists()
        assert any(stage.startswith("Creating and checking analysis proxy:") for stage in stages)
        assert "Measuring frame changes" in stages
        assert stages.count("Measuring frame changes") == 1
        assert window.progress_bar.maximum() == 100
        if with_music:
            verify_output(root / "previews" / "timelapse-001.mp4", 180, 30, expected_audio=True)
        else:
            assert not list((root / "previews").glob("*.mp4"))
            assert "Rendering preview" not in stages
            assert stages.count("Rendering MP4 from originals") == 1
        expected_frames = output_frame_count(saved.settings, 30, 180)
        verify_output(
            root
            / "renders"
            / ("timelapse-001_timelapse.mp4" if with_music else "Моя модель_timelapse.mp4"),
            expected_frames,
            30,
            expected_audio=with_music,
        )
        if with_music:
            assert saved.settings.music_folder == folder
            assert saved.settings.music_selection_mode == "full_tracks"
            assert saved.settings.music_seed == window.music_seed
            # Export advances the music seed; the subsequent preview is a new variant.
            import json

            mixes = list((root / "music").glob("*.flac"))
            assert len(mixes) == 2
            seeds = {
                json.loads(p.with_suffix(".json").read_text(encoding="utf-8"))["seed"]
                for p in mixes
            }
            assert len(seeds) == 2
        window.target.setValue(20)
        monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(root))
        window.choose_folder()
        assert window.target.value() == 0.1, "Saved seconds must load as minutes"
        window.player.play()
        for _ in range(20):
            QTest.qWait(20)
            time.sleep(0.001)
        assert window.video_frames_seen > 0
        assert window.grab().save(str(tmp_path / "window.png"))
        monkeypatch.setattr(window, "confirm_action", lambda *_: True)
        window.start_action("preview")
        window.cancel_button.click()
        deadline = time.monotonic() + 20
        while window.job is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert window.job is None
        assert "Cancelled" in window.last_error

    finally:
        timer.stop()
        if window.job:
            window.cancel_job()
            window.job.wait(60000)
            app.processEvents()
        window.close()
        app.processEvents()


def test_cancel_and_close_require_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    try:
        job = Job(lambda cancel, progress, stage: "")
        window.job = job
        window.cancel_button.setEnabled(True)
        prompts: list[tuple[str, str, str, str]] = []

        def reject(*args: str) -> bool:
            prompts.append(args)
            return False

        monkeypatch.setattr(window, "confirm_action", reject)
        window.cancel_job()
        assert not job.cancel.is_set()
        close_event = QCloseEvent()
        window.handle_close_request(close_event)
        assert not close_event.isAccepted()
        assert not window.close_after_job
        assert [prompt[0] for prompt in prompts] == ["Отменить обработку?", "Закрыть SculptCut?"]

        monkeypatch.setattr(window, "confirm_action", lambda *_: True)
        window.cancel_job()
        assert job.cancel.is_set()
        assert not window.cancel_button.isEnabled()

        closing_job = Job(lambda cancel, progress, stage: "")
        window.job = closing_job
        closing_event = QCloseEvent()
        window.handle_close_request(closing_event)
        assert closing_job.cancel.is_set()
        assert window.close_after_job
        assert not closing_event.isAccepted()
    finally:
        window.close()
        app.processEvents()


def test_idle_window_closes_without_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    prompts: list[str] = []
    monkeypatch.setattr(window, "confirm_action", lambda title, *_: prompts.append(title))
    event = QCloseEvent()
    window.handle_close_request(event)
    assert event.isAccepted()
    assert prompts == []

    changed = MainWindow()
    changed.output_name.setText("Модель")
    monkeypatch.setattr(changed, "confirm_action", lambda title, *_: prompts.append(title) or False)
    changed_event = QCloseEvent()
    changed.handle_close_request(changed_event)
    assert not changed_event.isAccepted()
    assert prompts == ["Закрыть SculptCut?"]
    window.close()
    changed.close()
    app.processEvents()


def test_confirmation_dialog_defaults_to_the_safe_action() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    try:

        def inspect_and_reject() -> None:
            dialog = app.activeModalWidget()
            assert isinstance(dialog, QMessageBox)
            assert dialog.defaultButton() is not None
            assert dialog.defaultButton().text() == "Продолжить обработку"
            assert dialog.escapeButton() is dialog.defaultButton()
            assert {button.text() for button in dialog.buttons()} == {
                "Продолжить обработку",
                "Отменить обработку",
            }
            dialog.defaultButton().click()

        QTimer.singleShot(0, inspect_and_reject)
        assert not window.confirm_action(
            "Отменить обработку?",
            "Тестовое сообщение.",
            "Отменить обработку",
            "Продолжить обработку",
        )
    finally:
        window.close()
        app.processEvents()


def test_intro_image_picker_shows_selected_model_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    try:
        image_path = tmp_path / "модель.png"
        image = QPixmap(80, 60)
        image.fill(Qt.GlobalColor.cyan)
        assert image.save(str(image_path))
        monkeypatch.setattr(
            QFileDialog, "getOpenFileName", lambda *args: (str(image_path), "Images")
        )
        window.choose_intro_image()
        assert Path(window.intro_image.text()) == image_path.resolve()
        assert window.viewer_context.text() == "ЕЩЁ НЕ СОЗДАН"
        assert window.intro_preview.pixmap() is not None
        assert not window.intro_preview.pixmap().isNull()
        assert "готовый ролик" in window.video.text()
        assert "будет добавлена при создании ролика" in window.status.text()
        assert "обработка не запущена" in window.progress_details.text()
    finally:
        window.close()
        app.processEvents()


def test_intro_picker_pauses_but_preserves_playing_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = QApplication.instance() or QApplication([])
    video = tmp_path / "preview.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=160x120:r=15:d=5",
            "-c:v",
            "libx264",
            str(video),
        ]
    )
    image_path = tmp_path / "новая заставка.png"
    image = QPixmap(80, 60)
    image.fill(Qt.GlobalColor.cyan)
    assert image.save(str(image_path))
    window = MainWindow()
    window.show()
    try:
        window.open_video(video, "Сохранённый предпросмотр", autoplay=True)
        for _ in range(100):
            if window.video_frames_seen and window.player.position() > 0:
                break
            QTest.qWait(20)
        source = window.player.source()
        position = window.player.position()
        title = window.video_title.text()
        context = window.viewer_context.text()
        monkeypatch.setattr(
            QFileDialog, "getOpenFileName", lambda *args: (str(image_path), "Images")
        )
        window.choose_intro_image()
        assert window.player.source() == source
        assert abs(window.player.position() - position) < 100
        assert window.player.playbackState() == QMediaPlayer.PlaybackState.PausedState
        assert window.play_button.text() == "Просмотр"
        assert not window.play_button.icon().isNull()
        assert window.video_title.text() == title
        assert window.viewer_context.text() == context
        assert "Видео поставлено на паузу" in window.status.text()
        assert "обработка не запущена" in window.progress_details.text()
        window.play_pause()
        for _ in range(50):
            if window.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                break
            QTest.qWait(20)
        assert window.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        assert window.play_button.text() == "Пауза"
        assert not window.play_button.icon().isNull()
        assert window.player.source() == source
    finally:
        window.close()
        app.processEvents()
