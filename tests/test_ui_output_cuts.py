"""Saving legacy shorts must still support ordinary non-destructive trimming."""

import json
from pathlib import Path
from threading import Event
from typing import Literal

import pytest
from PySide6.QtCore import QEventLoop, Qt, QTimer, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from sculptcut.cut_save_dialog import CutSaveDialog
from sculptcut.domain import ProjectSettings
from sculptcut.export_storage import metadata_path
from sculptcut.media import discover, run_process
from sculptcut.planning import build_plan
from sculptcut.project import create_project, fingerprint, relink_sources, save_project
from sculptcut.rendering import render, verify_output
from sculptcut.ui import MainWindow
from sculptcut.variants import read_variant


@pytest.mark.parametrize(("preset", "history"), [
    ("shorts", "missing"),
    ("shorts", "null"),
    ("shorts", "empty"),
    ("photoshop_shorts", "null"),
    ("shorts", "cut"),
    ("shorts", "render"),
    ("shorts", "relocated"),
    ("shorts", "plain"),
    ("photoshop_shorts", "plain"),
    ("photoshop_shorts", "render"),
    ("music_shorts", "plain"),
    ("beat_shorts", "plain"),
    ("photoshop_beat_shorts", "plain"),
    ("photoshop_music_shorts", "render"),
    ("timelapse", "render"),
    ("speed", "render"),
    ("music_shorts", "render"),
    ("beat_shorts", "render"),
    ("photoshop_beat_shorts", "render"),
    ("shorts", "insufficient"),
    ("shorts", "cancel"),
])
def test_save_short_cuts_obeys_explicit_choice(
    tmp_path: Path, preset: Literal[
        "timelapse", "speed", "shorts", "photoshop_shorts", "music_shorts",
        "beat_shorts", "photoshop_beat_shorts", "photoshop_music_shorts",
    ], history: str,
) -> None:
    app = QApplication.instance() or QApplication([])
    source = tmp_path / "source.mp4"
    run_process([
        discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
        "testsrc2=s=160x120:r=30:d=1", "-c:v", "libx264", str(source),
    ])
    root = tmp_path / "project"
    project = create_project(root, [source]).model_copy(
        update={"settings": ProjectSettings(preset=preset)},
    )
    save_project(root, project)
    video = root / "renders/short.mp4"
    render(root, project, build_plan(project, [], 0.6), video, Event(), encoder="libx264")
    state = read_variant(video)
    if history == "missing":
        state.pop("plan")
    elif history in ("null", "empty"):
        state["plan"] = None if history == "null" else {}
    elif history == "cut":
        state.update(operation="cut", cuts_us=[[0, 100_000]])
    snapshot = metadata_path(video, ".variant.json")
    snapshot.write_text(json.dumps(state), encoding="utf-8")
    original_video = fingerprint(video)
    original_snapshot = snapshot.read_bytes()
    moved = tmp_path / "перенесённое видео.mp4"
    if history == "relocated":
        source.rename(moved)
        relink_sources(root, {project.sources[0].id: moved})
        assert not source.exists()

    window = MainWindow()
    try:
        window.folder.setText(str(root))
        window.show()
        window.player.setSource(QUrl.fromLocalFile(str(video)))
        for _ in range(100):
            if window.player.duration() == 600:
                break
            QTest.qWait(20)
        assert window.player.duration() == 600
        window.output_editor.set_duration(600)
        cuts = [(0, 500_000)] if history == "insufficient" else [(200_000, 400_000)]
        window.output_editor.cuts = list(cuts)
        refilled = history in ("render", "relocated")
        choice_errors: list[str] = []

        def choose() -> None:
            dialog = window.findChild(CutSaveDialog)
            assert dialog is not None
            try:
                assert dialog.isVisible()
                assert dialog.trim.isChecked() and not dialog.keep_duration
                can_refill = history in ("plain", "render", "relocated", "cancel", "cut")
                assert dialog.refill.isEnabled() is can_refill
                assert dialog.reason.isVisible() is not can_refill
                if history == "insufficient":
                    assert "не хватает оставшегося исходного материала" in dialog.reason.text()
                    assert "минут" not in dialog.reason.text().lower()
                if history == "cancel":
                    QTest.mouseClick(dialog.cancel_button, Qt.MouseButton.LeftButton)
                    return
                if refilled:
                    QTest.mouseClick(dialog.refill, Qt.MouseButton.LeftButton)
                    assert dialog.keep_duration
                QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
            except Exception as exc:
                choice_errors.append(str(exc))
                dialog.reject()

        QTimer.singleShot(0, choose)
        window.save_output_cuts()
        assert not choice_errors, choice_errors
        if history == "cancel":
            assert window.job is None
            assert window.output_editor.cuts == cuts
            assert not video.with_name("short-edit-001.mp4").exists()
            assert fingerprint(video) == original_video
            assert snapshot.read_bytes() == original_snapshot
            return
        if window.job is not None:
            loop = QEventLoop()
            timeout = QTimer()
            timeout.setSingleShot(True)
            timeout.timeout.connect(loop.quit)
            window.job.finished.connect(loop.quit)
            timeout.start(30_000)
            loop.exec()
            timeout.stop()
        assert window.job is None, "Saving cuts did not finish"
        assert window.job_succeeded, window.last_error
        result = video.with_name("short-edit-001.mp4")
        expected = 3 if history == "insufficient" else 18 if refilled else 12
        verify_output(result, expected, 30)
        saved = read_variant(result)
        assert saved["operation"] == ("duration_refill" if refilled else "cut")
        assert saved["parent"] == video.name
        assert saved["cuts_us"] == [list(cut) for cut in cuts]
        if history == "relocated":
            assert saved["project"]["sources"][0]["path"] == str(moved.resolve())
        assert ("длительность сохранена" in window.export_note) is refilled
        assert fingerprint(video) == original_video
        assert snapshot.read_bytes() == original_snapshot
    finally:
        if window.job is not None:
            window.job.cancel.set()
            window.job.wait(10_000)
            app.processEvents()
        window.close()
        app.processEvents()
