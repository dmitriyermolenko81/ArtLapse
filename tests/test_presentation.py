from pathlib import Path
from threading import Event

import cv2
import pytest

from sculptcut.domain import Project, ProjectSettings
from sculptcut.intro import body_start_frame, intro_frames, intro_signature, output_frame_count
from sculptcut.media import Cancelled, discover, probe, run_process
from sculptcut.planning import build_plan, validate_plan
from sculptcut.presentation import attach_presentation
from sculptcut.project import create_project, fingerprint, save_project
from sculptcut.proxy import make_proxies
from sculptcut.rendering import render, verify_output
from sculptcut.variants import read_variant


@pytest.fixture
def clips(tmp_path: Path) -> tuple[Path, Path]:
    body, clip = tmp_path / "монтаж.mp4", tmp_path / "turnaround's 360.mp4"
    for path, filters in ((body, "color=c=blue:s=160x90:r=30:d=2"),
                          (clip, "color=c=red:s=120x160:r=24:d=1")):
        audio = (["-f", "lavfi", "-i", "sine=frequency=880:duration=1", "-c:a", "aac"]
                 if path == clip else [])
        run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", filters,
                     *audio,
                     "-vf", "drawbox=c=lime:t=fill:enable='gte(t,0.5)'",
                     "-c:v", "libx264", str(path)])
    return body, clip


@pytest.mark.parametrize(("position", "seconds"), [("start", .5), ("end", 2)])
def test_full_turnaround_retimed_and_placed_without_changing_originals(
    clips: tuple[Path, Path], tmp_path: Path, position: str, seconds: float,
) -> None:
    body, clip = clips
    before = [fingerprint(p) for p in clips]
    settings = ProjectSettings(presentation_video=clip, presentation_seconds=seconds,
                               presentation_position=position)
    output = tmp_path / "result.mp4"
    attach_presentation(body, output, settings, width=160, height=90, fps=30,
                        encoder="libx264")
    count = output_frame_count(settings, 30, 60)
    verify_output(output, count, 30)
    capture = cv2.VideoCapture(str(output))
    try:
        offset = 0 if position == "start" else 60
        for index, channel in ((offset, 2), (offset + round(seconds * 30) - 1, 1)):
            capture.set(cv2.CAP_PROP_POS_FRAMES, index)
            ok, frame = capture.read()
            assert ok
            means = frame[20:70, 60:100].mean(axis=(0, 1))
            assert means[channel] > 150 and means[channel] > means[(channel + 1) % 3] * 2
    finally:
        capture.release()
    assert [fingerprint(p) for p in clips] == before


@pytest.mark.parametrize("position", ["start", "end"])
def test_render_preview_music_intro_snapshot_and_plan_invalidation(
    clips: tuple[Path, Path], tmp_path: Path, position: str, caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import logging

    from test_intro import make_image

    body, clip = clips
    monkeypatch.setattr(logging.getLogger("artlapse"), "propagate", True)
    caplog.set_level("INFO", logger="artlapse")
    root = tmp_path / "project"
    project = create_project(root, [body])
    image, music = tmp_path / "cover.png", tmp_path / "music.wav"
    make_image(image, (220, 220, 220))
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 "sine=frequency=440:duration=4", str(music)])
    settings = ProjectSettings(preset="speed", intro_image=image, intro_seconds=.5,
                               intro_transition_seconds=.1, music_path=music,
                               presentation_video=clip, presentation_seconds=.5,
                               presentation_position=position)
    project = project.model_copy(update={"settings": settings})
    save_project(root, project)
    plan = build_plan(project, [], 1)
    assert body_start_frame(settings, 30, 30) == (15 if position == "start" else 0)
    expected = output_frame_count(settings, 30, plan.actual_frame_count)
    assert expected == 45
    make_proxies(root, project)
    output = render(root, project, plan, tmp_path / "export.mp4", encoder="libx264")
    verify_output(output, expected, 30, expected_audio=True)
    preview = render(root, project, plan, tmp_path / "preview.mp4", preview=True)
    verify_output(preview, expected, 30, expected_audio=True)
    encoders = [record.getMessage() for record in caplog.records
                if record.getMessage().startswith("Final video encoder:")]
    assert encoders == ["Final video encoder: libx264"] * 2
    for video in (output, preview):
        capture = cv2.VideoCapture(str(video))
        try:
            # Presentation red, then green; montage starts blue. No white cover.
            for index, channel in ((0, 2 if position == "start" else 0),
                                   (14 if position == "start" else 44, 1),
                                   (15 if position == "start" else 30,
                                    0 if position == "start" else 2)):
                capture.set(cv2.CAP_PROP_POS_FRAMES, index)
                ok, frame = capture.read()
                assert ok
                h, w = frame.shape[:2]
                means = frame[h // 3:2 * h // 3, w // 2 - 10:w // 2 + 10].mean(axis=(0, 1))
                assert means[channel] > 150
                assert means[channel] > means[(channel + 1) % 3] * 2
        finally:
            capture.release()
    assert Project.model_validate(read_variant(output)["project"]).settings == settings
    changed = project.model_copy(update={"settings": settings.model_copy(update={
        "presentation_position": "end" if position == "start" else "start",
    })})
    with pytest.raises(ValueError, match="видео-презентация изменились"):
        validate_plan(changed, plan)
    clip.write_bytes(clip.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="видео-презентация изменились"):
        validate_plan(project, plan)


def test_inactive_image_is_ignored_and_restored_without_presentation(
    clips: tuple[Path, Path], tmp_path: Path,
) -> None:
    from test_intro import make_image

    from sculptcut.preflight import check_creation

    body, clip = clips
    image = tmp_path / "missing-cover.png"
    settings = ProjectSettings(preset="speed", intro_image=image, intro_seconds=.5,
                               intro_transition_seconds=.1, presentation_video=clip,
                               presentation_seconds=.5)
    root = tmp_path / "project"
    project = create_project(root, [body]).model_copy(update={"settings": settings})
    save_project(root, project)
    check_creation(root, [body], settings)
    signature = intro_signature(settings)
    assert intro_frames(settings, 30, 30) == (0, 0)
    assert signature == intro_signature(settings.model_copy(update={"intro_image": None}))
    plan = build_plan(project, [], 1)
    output = render(root, project, plan, tmp_path / "no-cover.mp4", encoder="libx264")
    verify_output(output, 45, 30)
    make_image(image, (220, 220, 220))
    assert intro_signature(settings) == signature
    restored = settings.model_copy(update={"presentation_video": None})
    assert intro_frames(restored, 30, 30) == (15, 3)
    assert output_frame_count(restored, 30, 30) == 42
    assert intro_signature(restored) != signature
    with pytest.raises(ValueError, match="видео-презентация изменились"):
        validate_plan(project.model_copy(update={"settings": restored}), plan)


def test_replace_intro_rejects_presentation_without_changing_variant(
    clips: tuple[Path, Path], tmp_path: Path,
) -> None:
    from test_intro import make_image

    from sculptcut.reintro import replace_intro
    from sculptcut.variants import save_variant

    body, clip = clips
    root = tmp_path / "project"
    settings = ProjectSettings(preset="speed", presentation_video=clip, presentation_seconds=.5)
    project = create_project(root, [body]).model_copy(update={"settings": settings})
    save_project(root, project)
    output = root / "renders" / "original.mp4"
    output.write_bytes(body.read_bytes())
    save_variant(output, project=project, plan=build_plan(project, [], 1))
    image = tmp_path / "cover.png"
    make_image(image, (220, 220, 220))
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="Отключите презентацию"):
        replace_intro(root, output, image, None, encoder="libx264")
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_gui_creation_accepts_missing_inactive_image(
    clips: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from time import monotonic, sleep

    from PySide6.QtWidgets import QApplication

    from sculptcut.ui import MainWindow

    app = QApplication.instance() or QApplication([])
    body, clip = clips
    window = MainWindow()
    continued: list[str] = []
    monkeypatch.setattr(window, "_start_checked_action", continued.append)
    try:
        window.show()
        window.folder.setText(str(tmp_path / "project"))
        window.set_sources([body, clip])
        window.intro_image.setText(str(tmp_path / "missing-cover.png"))
        window.restore_presentation(ProjectSettings(presentation_video=clip))
        window.output_name.setText("priority")
        window.preset.setCurrentIndex(window.preset.findData("speed"))
        window.create_button.click()
        assert window.job is not None and window.job.objectName() == "preflight"
        deadline = monotonic() + 20
        while (window.job or not continued) and monotonic() < deadline:
            app.processEvents()
            sleep(.01)
        assert continued == ["create"] and not window.last_error
        window.restore_presentation(ProjectSettings())
        window.create_button.click()
        assert window.job is None
        assert "изображение заставки" in window.last_error
    finally:
        if window.job:
            window.job.cancel.set()
            window.job.wait(20000)
        window.close()
        app.processEvents()


def test_square_fitting_and_cancel(clips: tuple[Path, Path], tmp_path: Path) -> None:
    body, clip = clips
    # Already-normalized vertical body, matching the Shorts canvas.
    vertical = tmp_path / "vertical.mp4"
    run_process([discover("ffmpeg"), "-v", "error", "-i", str(body), "-vf",
                 "scale=180:320,setsar=1", "-an", "-c:v", "libx264", str(vertical)])
    settings = ProjectSettings(preset="shorts", shorts_layout="square",
                               presentation_video=clip, presentation_seconds=.5)
    output = tmp_path / "square.mp4"
    attach_presentation(vertical, output, settings, width=180, height=320, fps=30,
                        encoder="libx264")
    verify_output(output, 75, 30)
    info = probe(output)
    assert (info.width, info.height) == (180, 320)
    capture = cv2.VideoCapture(str(output))
    try:
        ok, frame = capture.read()
        assert ok and 15 < frame[:60].mean() < 30  # Same flat bands as square Shorts.
    finally:
        capture.release()
    cancelled = Event()
    cancelled.set()
    with pytest.raises(Cancelled):
        attach_presentation(vertical, tmp_path / "cancelled.mp4", settings,
                            width=180, height=320, fps=30, encoder="libx264", cancel=cancelled)


def test_preflight_protects_presentation_and_refuses_bad_file(
    clips: tuple[Path, Path], tmp_path: Path,
) -> None:
    from sculptcut.preflight import check_creation
    from sculptcut.project_cleanup import cleanup_files
    from sculptcut.project_deletion import project_deletion_target

    body, clip = clips
    root = tmp_path / "project"
    project = create_project(root, [body])
    protected_clip = root / "renders" / "presentation.mp4"
    protected_clip.write_bytes(clip.read_bytes())
    settings = ProjectSettings(preset="speed", presentation_video=protected_clip)
    save_project(root, project.model_copy(update={"settings": settings}))
    check_creation(root, [body], settings)
    assert protected_clip not in {entry.path for entry in cleanup_files(root)}
    with pytest.raises(ValueError):
        project_deletion_target(root)
    protected_clip.write_bytes(b"broken video")
    with pytest.raises((ValueError, RuntimeError)):
        check_creation(root, [body], settings)
    assert protected_clip.is_file() and (root / "project.json").is_file()


@pytest.mark.parametrize("position", ["start", "end"])
def test_music_clock_and_manual_cut_match_the_body_after_presentation(
    clips: tuple[Path, Path], tmp_path: Path, position: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_timeline import make_source

    from sculptcut.audio_beat_sync import MusicAnalysis, MusicEvent
    from sculptcut.music_episodes import music_events
    from sculptcut.music_waveform import output_episodes
    from sculptcut.shorts_refill import refill_plan_after_cuts

    _, clip = clips
    settings = ProjectSettings(preset="shorts", presentation_video=clip,
                               presentation_position=position, presentation_seconds=5,
                               intro_image=tmp_path / "missing-cover.png",
                               music_path=clip, beat_sync_enabled=True)
    project = Project(sources=[make_source(0, 0, 120_000_000)], settings=settings)
    plan = build_plan(project, [], 60)
    durations: list[float] = []

    def music(_root: Path, _settings: ProjectSettings, seconds: float, _cancel: object) -> Path:
        durations.append(seconds)
        return clip

    monkeypatch.setattr("sculptcut.music_episodes.soundtrack", music)
    monkeypatch.setattr("sculptcut.music_episodes.analyze_music",
                        lambda *a, **kw: MusicAnalysis(120, (), ()))
    monkeypatch.setattr("sculptcut.music_episodes.select_cut_beats",
                        lambda *a, **kw: [MusicEvent(10, 1)])
    _, offset = music_events(tmp_path, project, 1800, None)
    assert durations == [65] and offset == (5 if position == "start" else 0)
    assert output_episodes(settings, plan)[0].start == offset
    cuts = [(round((offset + 10) * 1e6), round((offset + 15) * 1e6))]
    rebuilt = refill_plan_after_cuts(project, plan, cuts)
    removed = [d for d in rebuilt.decisions if d.provenance == "user:shorts_manual_cut"]
    assert [(d.source_start_us, d.source_end_us) for d in removed] == [(20_000_000, 30_000_000)]
    outside = (0, 1_000_000) if position == "start" else (60_000_000, 61_000_000)
    with pytest.raises(ValueError, match="Презентацию измените"):
        refill_plan_after_cuts(project, plan, [outside])


def test_adding_image_after_presentation_preserves_successive_cut_clocks() -> None:
    from sculptcut.reintro import shift_intro_history

    assert shift_intro_history([[(1, 2)], [(4, 6)]], prefix=5, shift=2) == [
        [(1, 2)], [(6, 8)],
    ]
    assert shift_intro_history([[(4, 6)], [(4, 5)]], prefix=5, shift=2) == [
        [(4, 8)], [(4, 5)],
    ]


def test_gui_assign_cancel_save_restore_and_session(
    clips: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    from time import monotonic, sleep

    from PySide6.QtCore import QCoreApplication, QEvent, QTimer
    from PySide6.QtWidgets import QApplication
    from test_intro import make_image

    from sculptcut.presentation_dialog import PresentationDialog
    from sculptcut.project import load_project
    from sculptcut.source_cards import PRESENTATION_ROLE
    from sculptcut.ui import MainWindow
    from sculptcut.variants import save_variant

    app = QApplication.instance() or QApplication([])
    body, clip = clips
    root = tmp_path / "project"
    image = tmp_path / "cover.png"
    make_image(image, (220, 220, 220))
    window = MainWindow()
    monkeypatch.setattr(window, "confirm_action", lambda *args: True)
    try:
        window.folder.setText(str(root))
        window.set_sources([body, clip])
        window.intro_image.setText(str(image))
        window.show()
        app.processEvents()

        def configure(accept: bool) -> None:
            dialog = next(d for d in window.findChildren(PresentationDialog) if d.isVisible())
            assert isinstance(dialog, PresentationDialog)
            dialog.video.setCurrentIndex(dialog.video.findData(str(clip.resolve())))
            dialog.seconds.setValue(6)
            dialog.position.setCurrentIndex(dialog.position.findData("end"))
            dialog.apply_button.click() if accept else dialog.reject()

        QTimer.singleShot(20, lambda: configure(False))
        card = window.sources.item(1)
        window.sources.setCurrentRow(1)
        window.presentation_button.click()
        assert window.presentation_video is None and window.sources.count() == 2
        QTimer.singleShot(20, lambda: configure(True))
        window.presentation_button.click()
        assert window.presentation_video == clip.resolve()
        assert window.intro_image.text() == str(image)
        assert not window.intro_image.isEnabled()
        assert not window.intro_preview.isEnabled()
        assert not window.change_intro_button.isEnabled()
        assert window.intro_label.text() == "Заставка · не используется"
        window.language_button.click()
        assert window.intro_label.text() == "Intro image · not used"
        assert "Turn it off" in window.intro_image.toolTip()
        window.language_button.click()
        assert window.source_paths() == [body.resolve()]
        assert window.listed_source_paths() == [body.resolve(), clip.resolve()]
        assert card.data(PRESENTATION_ROLE) == "В конце · 6 с"
        assert window.sources.currentItem() == card
        assert window.selected_montage_source() == body.resolve()
        for preset in ("speed", "shorts", "music_shorts", "beat_shorts", "timelapse"):
            window.preset.setCurrentIndex(window.preset.findData(preset))
            assert window.presentation_seconds == 6 and window.presentation_position == "end"
        window.save_current_project()
        deadline = monotonic() + 10
        while window.job is not None and monotonic() < deadline:
            app.processEvents()
            sleep(.01)
        assert window.job is None and not window.last_error
        saved = load_project(root)
        assert saved.settings.presentation_video == clip.resolve()
        assert saved.settings.intro_image == image
        assert not window.intro_image.isEnabled()  # Job completion must keep priority.
        assert saved.settings.presentation_seconds == 6
        assert saved.settings.presentation_position == "end"
        assert saved.settings.presentation_source_index == 1
        assert len(saved.sources) == 1
        draft = window.form_draft()
        window.restore_presentation(ProjectSettings())
        window.open_project_folder(root)
        assert window.presentation_video == clip.resolve()
        assert window.intro_image.text() == str(image) and not window.intro_image.isEnabled()
        assert window.listed_source_paths() == [body.resolve(), clip.resolve()]
        video = root / "renders" / "version.mp4"
        video.write_bytes(body.read_bytes())
        save_variant(video, project=saved, plan=build_plan(saved, [], 1))
        window.refresh_variants()
        window.restore_presentation(ProjectSettings())
        window.restore_variant()
        assert window.presentation_video == clip.resolve() and window.presentation_seconds == 6
        window.restore_presentation(ProjectSettings())
        window._recover_after_crash = True
        window.recent_settings.setValue("session_draft", json.dumps(draft))
        window.recover_session()
        assert window.presentation_video == clip.resolve() and window.presentation_position == "end"
        assert window.listed_source_paths() == [body.resolve(), clip.resolve()]

        def disable() -> None:
            dialog = next(d for d in window.findChildren(PresentationDialog) if d.isVisible())
            assert isinstance(dialog, PresentationDialog)
            dialog.video.setCurrentIndex(0)
            dialog.apply_button.click()

        QTimer.singleShot(20, disable)
        window.presentation_button.click()
        assert window.presentation_video is None
        assert window.intro_image.isEnabled() and window.intro_preview.isEnabled()
        assert window.intro_image.text() == str(image)
        assert window.intro_label.text() == "Заставка"
        assert window.source_paths() == [body.resolve(), clip.resolve()]

        QTimer.singleShot(20, lambda: configure(True))
        window.presentation_button.click()
        window.sources.setCurrentRow(1)
        window.remove_sources()
        assert window.presentation_video is None
        assert window.listed_source_paths() == [body.resolve()]
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not window.findChildren(PresentationDialog)
    finally:
        window.close()
        app.processEvents()


def test_legacy_presentation_card_role_switch_and_presentation_only_guard(
    clips: tuple[Path, Path], tmp_path: Path,
) -> None:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from sculptcut.presentation_dialog import PresentationDialog
    from sculptcut.source_cards import PRESENTATION_ROLE
    from sculptcut.ui import MainWindow

    app = QApplication.instance() or QApplication([])
    body, clip = clips
    root = tmp_path / "legacy"
    project = create_project(root, [body])
    legacy = ProjectSettings.model_validate({"presentation_video": str(clip)})
    save_project(root, project.model_copy(update={"settings": legacy}))
    window = MainWindow()
    try:
        window.show()
        window.open_project_folder(root)
        assert window.listed_source_paths() == [clip.resolve(), body.resolve()]
        assert window.source_paths() == [body.resolve()]
        assert window.sources.item(0).data(PRESENTATION_ROLE)

        def assign_body() -> None:
            dialog = next(d for d in window.findChildren(PresentationDialog) if d.isVisible())
            assert isinstance(dialog, PresentationDialog)
            dialog.video.setCurrentIndex(dialog.video.findData(str(body.resolve())))
            dialog.apply_button.click()

        QTimer.singleShot(20, assign_body)
        window.presentation_button.click()
        assert window.presentation_video == body.resolve()
        assert window.source_paths() == [clip.resolve()]
        assert not window.sources.item(0).data(PRESENTATION_ROLE)
        assert window.sources.item(1).data(PRESENTATION_ROLE)
        # Reordering cards retains the role and saves its new list position.
        item = window.sources.takeItem(1)
        window.sources.insertItem(0, item)
        assert window.presentation_values()["presentation_source_index"] == 0
        window.sources.setCurrentRow(1)
        window.remove_sources()
        assert window.listed_source_paths() == [body.resolve()]
        assert not window.source_paths()
        window.save_current_project()
        assert window.job is None and window.last_error
    finally:
        window.close()
        app.processEvents()


def test_presentation_dialog_cached_and_late_preview(tmp_path: Path) -> None:
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtWidgets import QApplication, QWidget

    from sculptcut.presentation_dialog import PresentationDialog
    from sculptcut.ui import STYLE

    app = QApplication.instance() or QApplication([])
    first, second = tmp_path / "first.mp4", tmp_path / "second.mp4"
    first.touch()
    second.touch()
    frame = QImage(160, 90, QImage.Format.Format_RGB32)
    frame.fill(QColor("#fe8700"))
    parent = QWidget()
    parent.setStyleSheet(STYLE)
    dialog = PresentationDialog(first, 5, "start", [first, second], parent,
                                media={str(first): (frame, "00:07 · 1920×1080")})
    try:
        dialog.show()
        app.processEvents()
        assert dialog.thumbnail.pixmap().toImage().pixelColor(20, 20) == QColor("#fe8700")
        dialog.position.group.button(1).click()
        assert dialog.placement == "end"
        dialog.video.setCurrentIndex(dialog.video.findData(str(second)))
        placeholder = dialog.thumbnail.pixmap().cacheKey()
        dialog.set_source_media(str(first), frame, "obsolete selection")
        assert dialog.thumbnail.pixmap().cacheKey() == placeholder
        frame.fill(QColor("#0066ee"))
        dialog.set_source_media(str(second), frame, "00:06 · 1280×720")
        assert dialog.thumbnail.pixmap().toImage().pixelColor(20, 20) == QColor("#0066ee")
        second.unlink()
        dialog.apply_button.click()
        assert dialog.isVisible() and "Файл не найден" in dialog.note.text()
        dialog.video.setCurrentIndex(0)
        assert not dialog.position.isEnabled() and not dialog.seconds.isEnabled()
        assert dialog.thumbnail.isHidden() and dialog.note.isHidden()
        dialog.apply_button.click()
        assert dialog.result() == dialog.DialogCode.Accepted and dialog.path is None
    finally:
        dialog.close()
        parent.close()
        app.processEvents()
