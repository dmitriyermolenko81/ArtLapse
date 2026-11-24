import json
from pathlib import Path
from threading import Event

import pytest
from test_combined_edits import fixture_project

from sculptcut.combined_edits import prepare_combined_edit, render_combined_edit
from sculptcut.drafts import ensure_draft
from sculptcut.export_storage import metadata_path
from sculptcut.media import discover, run_process
from sculptcut.rendering import verify_output
from sculptcut.review_copy import copy_review
from sculptcut.review_presentation import update_review_presentation
from sculptcut.source_selection import SourceSelection
from sculptcut.variants import read_variant


def test_review_add_replace_export_and_proxy_fallback(tmp_path: Path) -> None:
    root, _, _, video = fixture_project(tmp_path)
    clip = tmp_path / "opening.mp4"
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 "color=red:s=80x60:r=25:d=1", "-c:v", "libx264", str(clip)])
    cancel = Event()
    preview = update_review_presentation(root, video, clip, 1, "start", cancel)
    verify_output(preview, 120, 30)
    replaced = update_review_presentation(root, preview, clip, .5, "end", cancel)
    verify_output(replaced, 105, 30)
    final = root / "renders" / "reviewed.mp4"
    assert copy_review(root, replaced, final, cancel, lambda _: None, draft=False)
    assert final.read_bytes() == replaced.read_bytes()
    assert not read_variant(final)["plan"]["settings"]["draft"]
    state = read_variant(replaced)
    state["plan"]["settings"].pop("preview_quality")
    metadata_path(replaced, ".variant.json").write_text(json.dumps(state), encoding="utf-8")
    assert not copy_review(root, replaced, final.with_stem("proxy"), cancel,
                           lambda _: None, draft=False)


def test_shorten_in_fixed_editor_undo_redo_and_apply(tmp_path: Path, monkeypatch) -> None:
    from sculptcut.episode_cuts import episode_cut_ranges

    root, _, _, video = fixture_project(tmp_path)
    cancel = Event()
    draft = ensure_draft(root, video, cancel, lambda _: None)
    session = SourceSelection(prepare_combined_edit(root, draft, exact_variant=True),
                              fixed_duration=True)
    # A split and deletion in a single Apply must reuse the unsplit parent.
    first = next(d for d in session.plan.decisions if d.output_frame_count)
    if first.output_frame_count == session.plan.actual_frame_count:
        assert session.split_at(1000)
    scene = next(d for d in session.plan.decisions if d.output_frame_count)
    session.select(scene.source_id, scene.source_start_us, scene.source_end_us, None, shorten=True)
    assert not session.keep_duration
    session.undo()
    assert session.keep_duration
    session.undo(redo=True)
    assert not session.keep_duration
    edit = session.export_edit()
    assert episode_cut_ranges(draft, edit.project, edit.plan)
    def unexpected(*args, **kwargs):
        raise AssertionError("Deletion should use the reviewed frames")
    monkeypatch.setattr("sculptcut.combined_edits.render", unexpected)
    output = root / "previews" / "shortened.mp4"
    render_combined_edit(root, edit, output, cancel, lambda _: None, preview=True)
    verify_output(output, edit.plan.actual_frame_count, 30)

@pytest.mark.parametrize("action", ["add_only", "replace", "decline", "remove"])
def test_presentation_button_updates_review_and_export_copies_it(
    tmp_path: Path, action: str,
) -> None:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox
    from test_draft_workflow import wait_until

    from sculptcut.presentation_dialog import PresentationDialog
    from sculptcut.ui import MainWindow

    app = QApplication.instance() or QApplication([])
    root, project, _, video = fixture_project(tmp_path)
    source = project.sources[0].path
    replacement = tmp_path / "new-presentation.mp4"
    replacement.write_bytes(video.read_bytes())
    window = MainWindow()
    prompts = []
    try:
        window.show()
        window.open_project_folder(root)
        window.set_sources([source, replacement])
        window.show_result(str(video))
        wait_until(lambda: window.current_edit_video(root) == video)

        def answer_add() -> None:
            if isinstance(prompt := app.activeModalWidget(), QMessageBox):
                prompts.append(prompt.text())
                choice = "Только для новой сборки" if action == "add_only" else "Добавить и в ролик"
                button = next((b for b in prompt.buttons() if b.text() == choice), None)
                if button is not None:
                    button.click()
                else:
                    prompt.reject()

        def choose() -> None:
            dialog = window.findChild(PresentationDialog)
            if dialog:
                dialog.video.setCurrentIndex(dialog.video.findData(str(source)))
                dialog.seconds.setValue(1)
                QTimer.singleShot(60, answer_add)
                dialog.apply_button.click()

        QTimer.singleShot(100, choose)
        window.presentation_button.click()
        wait_until(lambda: window.job is None)
        assert not window.last_error
        reviewed = window.last_result_video
        assert prompts == ["Добавить презентацию и в открытый ролик?"]
        assert window.presentation_video == source
        if action == "add_only":
            assert reviewed == video
            assert read_variant(reviewed)["project"]["settings"]["presentation_video"] is None
            assert not list((root / "previews").glob("*.mp4"))
            return
        prompts.clear()
        assert reviewed.parent == root / "previews"
        assert read_variant(reviewed)["project"]["settings"]["presentation_video"] == str(source)
        verify_output(reviewed, 120, 30)

        def answer() -> None:
            if isinstance(prompt := app.activeModalWidget(), QMessageBox):
                prompts.append(prompt.text())
                choice = "Заменить и в ролике" if action == "replace" else "Только для новой сборки"
                button = next((b for b in prompt.buttons() if b.text() == choice), None)
                if button is not None:
                    button.click()
                else:
                    prompt.reject()

        def revise() -> None:
            dialog = next((d for d in window.findChildren(PresentationDialog)
                           if d.isVisible()), None)
            if dialog is not None:
                if action in ("replace", "decline"):
                    dialog.video.setCurrentIndex(dialog.video.findData(str(replacement)))
                    dialog.seconds.setValue(.5)
                    QTimer.singleShot(60, answer)
                    dialog.apply_button.click()
                else:
                    dialog.video.setCurrentIndex(0)
                    dialog.review_button.click()

        QTimer.singleShot(100, revise)
        window.presentation_button.click()
        wait_until(lambda: window.job is None)
        assert not window.last_error
        revised = window.last_result_video
        assert (revised == reviewed) == (action == "decline")
        assert prompts == ([] if action == "remove" else [
            "Заменить презентацию и в открытом ролике?"])
        saved = read_variant(revised)["project"]["settings"]["presentation_video"]
        expected = {"replace": str(replacement), "decline": str(source), "remove": None}
        assert saved == expected[action]
        verify_output(revised, {"replace": 105, "decline": 120, "remove": 90}[action], 30)
        if action != "remove":
            assert window.presentation_video == replacement
        window.export_button.click()
        wait_until(lambda: window.job is None)
        assert window.last_result_video.parent == root / "renders"
        assert window.last_result_video.read_bytes() == revised.read_bytes()
    finally:
        window._allow_close = True
        window.close()
        app.processEvents()


@pytest.mark.parametrize("choice", ["list", "both", "unrelated"])
def test_remove_source_offers_to_update_open_presentation(tmp_path: Path, choice: str) -> None:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox
    from test_draft_workflow import wait_until

    from sculptcut.ui import MainWindow

    app = QApplication.instance() or QApplication([])
    root, project, _, original = fixture_project(tmp_path)
    clip = tmp_path / "presentation.mp4"
    clip.write_bytes(original.read_bytes())
    reviewed = update_review_presentation(root, original, clip, 1, "start", Event())
    original_bytes = reviewed.read_bytes()
    source = project.sources[0].path
    window = MainWindow()
    timer = QTimer(window)
    timer.setSingleShot(True)
    prompts = []

    def answer() -> None:
        dialog = app.activeModalWidget()
        if isinstance(dialog, QMessageBox):
            prompts.append(dialog.text())
            caption = "Убрать и из ролика" if choice == "both" else "Только из списка"
            button = next((button for button in dialog.buttons() if button.text() == caption), None)
            if button is not None:
                button.click()
            else:
                dialog.reject()

    timer.timeout.connect(answer)
    try:
        window.show()
        window.open_project_folder(root)
        # The loaded version can contain a presentation even if the current
        # project role was already cleared.
        window.set_sources([source, clip])
        window.show_result(str(reviewed))
        wait_until(lambda: window.current_edit_video(root) == reviewed)
        window.sources.setCurrentRow(0 if choice == "unrelated" else 1)
        timer.start(40)
        window.remove_button.click()
        timer.stop()
        assert window.listed_source_paths() == ([clip] if choice == "unrelated" else [source])
        assert prompts == ([] if choice == "unrelated" else [
            "Убрать презентацию и из открытого ролика?"])
        wait_until(lambda: window.job is None)
        assert not window.last_error
        assert reviewed.read_bytes() == original_bytes and clip.is_file()
        result = window.last_result_video
        assert (result != reviewed) == (choice == "both")
        verify_output(result, 90 if choice == "both" else 120, 30)
    finally:
        timer.stop()
        window._allow_close = True
        window.close()
        app.processEvents()
