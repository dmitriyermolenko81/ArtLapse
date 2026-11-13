"""Cover placement, timing, persistence and real bookend exports."""

import json
from pathlib import Path
from threading import Event

import cv2
import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from test_intro import make_image

from sculptcut.domain import Project, ProjectSettings
from sculptcut.intro import body_frame_budget, body_start_frame, intro_signature, output_frame_count
from sculptcut.intro_dialog import IntroDialog
from sculptcut.media import discover, run_process
from sculptcut.output_cuts import cut_output
from sculptcut.planning import build_plan, validate_plan
from sculptcut.project import create_project, fingerprint, load_project, save_project
from sculptcut.reintro import replace_intro
from sculptcut.rendering import render, verify_output
from sculptcut.ui import MainWindow
from sculptcut.variants import cut_history, read_variant


@pytest.fixture
def media(tmp_path: Path) -> tuple[Path, Path, Path]:
    body, presentation, image = (tmp_path / name for name in
                                 ("source.mp4", "presentation.mp4", "cover.png"))
    for path, color in ((body, "blue"), (presentation, "red")):
        run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                     f"color=c={color}:s=160x90:r=30:d=3", "-c:v", "libx264", str(path)])
    make_image(image, (0, 220, 0))
    return body, presentation, image


def channel_at(path: Path, index: int) -> int:
    capture = cv2.VideoCapture(str(path))
    try:
        capture.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, frame = capture.read()
        assert ok
        means = frame[30:60, 50:110].mean(axis=(0, 1))
        assert means.max() > 150
        return int(means.argmax())
    finally:
        capture.release()


@pytest.mark.parametrize(("cover_position", "presentation_position"), [
    ("end", None), ("end", "start"), ("start", "end"), ("end", "end"),
])
def test_real_cover_bookends_fit_total_and_preserve_order(
    tmp_path: Path, media: tuple[Path, Path, Path], cover_position: str,
    presentation_position: str | None,
) -> None:
    body, clip, image = media
    before = [fingerprint(p) for p in media]
    settings = ProjectSettings.model_validate({
        "preset": "speed", "intro_image": image, "intro_position": cover_position,
        "intro_seconds": .8, "intro_transition_seconds": .2,
        "presentation_video": clip if presentation_position else None,
        "presentation_position": presentation_position or "start", "presentation_seconds": .5,
    })
    root = tmp_path / "project"
    project = create_project(root, [body]).model_copy(update={"settings": settings})
    frames = body_frame_budget(settings, 30, 3)
    plan = build_plan(project, [], frames / 30)
    output = render(root, project, plan, tmp_path / "result.mp4", encoder="libx264")
    verify_output(output, 90, 30)
    run_process([discover("ffmpeg"), "-v", "error", "-xerror", "-i", str(output),
                 "-f", "null", "-"])
    assert channel_at(output, 0) == (2 if presentation_position == "start" else
                                     1 if cover_position == "start" else 0)
    assert channel_at(output, 89) == (1 if cover_position == "end" else 2)
    offset = body_start_frame(settings, 30, frames)
    assert channel_at(output, offset + frames // 2) == 0
    assert output_frame_count(settings, 30, frames) == 90
    assert Project.model_validate(read_variant(output)["project"]).settings == settings
    moved = settings.model_copy(update={"intro_position": "start" if cover_position == "end"
                                       else "end"})
    assert intro_signature(moved) != intro_signature(settings)
    with pytest.raises(ValueError):
        validate_plan(project.model_copy(update={"settings": moved}), plan)
    assert [fingerprint(p) for p in media] == before


def test_move_and_resize_cover_keeps_manual_body_cuts(
    tmp_path: Path, media: tuple[Path, Path, Path],
) -> None:
    body, clip, image = media
    root = tmp_path / "project"
    settings = ProjectSettings(preset="speed", intro_image=image, intro_seconds=1,
                               intro_transition_seconds=.2, presentation_video=clip,
                               presentation_position="end", presentation_seconds=.5)
    project = create_project(root, [body]).model_copy(update={"settings": settings})
    save_project(root, project)
    plan = build_plan(project, [], 2)
    original = render(root, project, plan, root / "renders/original.mp4", encoder="libx264")
    # Cover prefix .8s; remove .4s of the body, leaving both bookends intact.
    trimmed = cut_output(original, [(1_000_000, 1_400_000)], Event(), lambda _: None)
    result = replace_intro(root, trimmed, image, None, encoder="libx264", intro_options={
        "intro_position": "end", "intro_seconds": 1.5, "intro_transition_seconds": .2,
    })
    assert cut_history(result) == [[(200_000, 600_000)]]
    verify_output(result, 102, 30)
    run_process([discover("ffmpeg"), "-v", "error", "-xerror", "-i", str(result),
                 "-f", "null", "-"])
    assert channel_at(result, 0) == 0 and channel_at(result, 101) == 1
    assert load_project(root).settings.intro_position == "end"
    assert load_project(root).settings.intro_seconds == 1.5
    assert read_variant(result)["plan"]["decisions"] == read_variant(original)["plan"]["decisions"]


def test_cover_dialog_cancel_apply_undo_save_and_recover(
    tmp_path: Path, media: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from time import monotonic, sleep

    body, clip, image = media
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    errors: list[str] = []
    try:
        root = tmp_path / "project"
        window.folder.setText(str(root))
        window.set_sources([body, clip])
        window.intro_image.setText(str(image))
        window.restore_presentation(ProjectSettings(presentation_video=clip))
        window.show()
        app.processEvents()
        before = window.template_options()

        def interact(accept: bool) -> None:
            dialog = window.findChildren(IntroDialog)[-1]
            try:
                assert dialog.isVisible()
                assert not dialog.position.group.button(0).isEnabled()
                assert dialog.placement == "end"
                assert dialog.preview.pixmap() and not dialog.preview.pixmap().isNull()
                dialog.seconds.setValue(.5)
                dialog.apply_button.click() if accept else dialog.reject()
            except Exception as exc:
                errors.append(repr(exc))
                dialog.reject()

        QTimer.singleShot(10, lambda: interact(False))
        window.intro_settings_button.click()
        assert window.template_options() == before
        QTimer.singleShot(10, lambda: interact(True))
        window.intro_settings_button.click()
        assert not errors
        assert window.intro_position == "end" and window.intro_seconds == .5
        assert window.intro_transition_seconds == .25
        window.history_controller.undo()
        # A legacy conflicting pair is resolved when applying form options.
        assert window.intro_seconds == 2
        window.history_controller.redo()
        assert window.intro_seconds == .5 and window.intro_position == "end"
        window.save_current_project()
        deadline = monotonic() + 15
        while window.job is not None and monotonic() < deadline:
            app.processEvents()
            sleep(.01)
        assert window.job is None and not window.last_error, window.last_error
        saved = load_project(root).settings
        assert saved.intro_position == "end" and saved.intro_seconds == .5
        assert saved.intro_transition_seconds == .25
        draft = window.form_draft()
        window.intro_seconds = 10
        window.open_project_folder(root)
        assert window.intro_seconds == .5
        window.intro_seconds = 8
        window._recover_after_crash = True
        monkeypatch.setattr(window, "confirm_action", lambda *args: True)
        window.recent_settings.setValue("session_draft", json.dumps(draft))
        window.recover_session()
        assert window.intro_seconds == .5 and window.intro_position == "end"
    finally:
        if window.job:
            window.job.cancel.set()
            window.job.wait(20000)
        window.close()
        app.processEvents()


def test_opening_presentation_disables_cover_and_dialog_browse_clear(
    media: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from PySide6.QtWidgets import QFileDialog

    from sculptcut.presentation_dialog import PresentationDialog

    body, clip, image = media
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    errors = []
    try:
        window.set_sources([body, clip])
        window.intro_image.setText(str(image))
        window.show()

        def assign() -> None:
            dialog = window.findChildren(PresentationDialog)[-1]
            try:
                dialog.video.setCurrentIndex(dialog.video.findData(str(clip)))
                dialog.apply_button.click()
            except Exception as exc:
                errors.append(repr(exc))
                dialog.reject()

        QTimer.singleShot(10, assign)
        window.presentation_button.click()
        assert not errors
        assert window.presentation_position == "start" and window.intro_position == "start"
        assert not window.intro_image.text()
        dialog = IntroDialog(None, 2, "end", window, opening_presentation=True)
        dialog.show()
        assert not dialog.seconds.isEnabled() and not dialog.position.isEnabled()

        def choose(*args: object) -> tuple[str, str]:
            assert dialog.property("nativeDialogOpen")
            return str(image), "Images"

        monkeypatch.setattr(QFileDialog, "getOpenFileName", choose)
        dialog.browse_button.click()
        assert not dialog.property("nativeDialogOpen")
        assert dialog.path == image and dialog.seconds.isEnabled()
        assert dialog.placement == "end" and not dialog.position.group.button(0).isEnabled()
        dialog.clear_button.click()
        assert dialog.path is None and not dialog.seconds.isEnabled()
        dialog.apply_button.click()
        assert dialog.result() == IntroDialog.DialogCode.Accepted
    finally:
        window.close()
        app.processEvents()
