"""Regressions found by the application audit, including real media and GUI jobs."""

import json
import time
from pathlib import Path

import cv2
import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from sculptcut.cli import main
from sculptcut.domain import Project, ProjectSettings
from sculptcut.jobs import reusable_chunk
from sculptcut.media import discover, run_process
from sculptcut.planning import RenderDecision, build_plan
from sculptcut.project import create_project, fingerprint, load_project, save_project
from sculptcut.proxy import make_proxies
from sculptcut.remusic import replace_music
from sculptcut.rendering import render, verify_output
from sculptcut.ui import MainWindow
from sculptcut.variants import read_variant, save_variant


@pytest.mark.parametrize("data", ["[]", "null", '"project"', "1"])
def test_corrupt_json_is_rejected_without_crashing(tmp_path: Path, data: str) -> None:
    (tmp_path / "project.json").write_text(data, encoding="utf-8")
    video = tmp_path / "video.mp4"
    video.with_suffix(".variant.json").write_text(data, encoding="utf-8")
    with pytest.raises(ValueError):
        load_project(tmp_path)
    with pytest.raises(ValueError):
        read_variant(video)
    (tmp_path / "chunk.mp4").write_bytes(b"incomplete")
    (tmp_path / "chunk.mp4.json").write_text(data, encoding="utf-8")
    assert not reusable_chunk(tmp_path, "chunk.mp4")
    assert main(["analyze", str(tmp_path)]) == 1
    assert (tmp_path / "project.json").read_text(encoding="utf-8") == data


def test_duration_rollovers_and_zero_in_real_window() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.show()
        app.processEvents()


        card = window.duration_card
        window.target.setValue((12 * 60 + 34) / 60)
        card.hours.stepDown()
        assert (card.hours.value(), card.minutes.value(), card.seconds.value()) == (0, 12, 34)
        window.target.setValue(34 / 60)
        card.minutes.stepDown()
        assert card.seconds.value() == 34
        window.target.setValue((59 * 60 + 59) / 60)
        QTest.mouseClick(card.seconds, Qt.MouseButton.LeftButton)
        QTest.keyClick(card.seconds, Qt.Key.Key_Up)
        assert (card.hours.value(), card.minutes.value(), card.seconds.value()) == (1, 0, 0)
        QTest.keyClick(card.seconds, Qt.Key.Key_Down)
        assert (card.hours.value(), card.minutes.value(), card.seconds.value()) == (0, 59, 59)
        window.shorts_mode.click()
        window.target.setValue(300)
        card.seconds.stepUp()
        assert (card.minutes.value(), card.seconds.value()) == (5, 0)
        window.target.setValue(0)
        card.seconds.stepDown()
        assert window.target.value() == 0
    finally:
        window.close()
        app.processEvents()


@pytest.mark.parametrize("fields", [
    {"project": []}, {"plan": "bad"}, {"music": []}, {"parent": []},
    {"cuts_us": None}, {"cuts_us": [[3, 1]]}, {"cuts_us": [["0", "1"]]},
    {"operation": "cut"},
])
def test_malformed_variant_fields_are_rejected(tmp_path: Path, fields: dict[str, object]) -> None:
    video = tmp_path / "video.mp4"
    video.with_suffix(".variant.json").write_text(
        json.dumps({"schema_version": 1} | fields), encoding="utf-8",
    )
    with pytest.raises(ValueError):
        read_variant(video)


def test_narrow_window_keeps_player_controls_visible_with_long_variant_name(
    tmp_path: Path,
) -> None:
    app = QApplication.instance() or QApplication([])
    root = tmp_path / "project"
    renders = root / "renders"
    renders.mkdir(parents=True)
    (renders / ("длинное имя " * 9 + "_photoshop_beat_shorts.mp4")).touch()
    window = MainWindow()
    try:
        window.folder.setText(str(root))
        window.video_title.setText("длинное имя " * 9)
        window.resize(1120, 700)
        window.show()
        app.processEvents()
        assert window.versions_panel.isVisible()
        assert window.viewer_scroll.widget().width() <= window.viewer_scroll.viewport().width()
        assert window.viewer_scroll.horizontalScrollBar().maximum() == 0
        assert window.video_title.width() >= 60
    finally:
        window.close()
        app.processEvents()


@pytest.mark.parametrize("preset", ["beat_shorts", "photoshop_beat_shorts", "music_shorts"])
def test_musical_rebuild_never_silently_restores_manually_cut_footage(
    tmp_path: Path, preset: str,
) -> None:
    from threading import Event

    from sculptcut.output_cuts import cut_output

    video = tmp_path / "original.mp4"
    make_video(video)
    folder = tmp_path / "music"
    folder.mkdir()
    run_process([
        discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
        "sine=frequency=440:duration=3", str(folder / "track.wav"),
    ])
    project = Project(sources=[], settings=ProjectSettings(preset=preset))
    save_project(tmp_path, project)
    save_variant(video, project=project)
    trimmed = cut_output(video, [(500_000, 1_500_000)], Event(), lambda value: None)
    result = replace_music(tmp_path, trimmed, folder, 1, None)
    verify_output(result, 30, 30, expected_audio=True)
    before = cv2.VideoCapture(str(trimmed))
    after = cv2.VideoCapture(str(result))
    try:
        while True:
            ok, frame = before.read()
            actual, changed = after.read()
            assert actual == ok
            if not ok:
                break
            assert np.array_equal(frame, changed)
    finally:
        before.release()
        after.release()
    assert read_variant(result)["cut_history_us"] == [[[500_000, 1_500_000]]]
    assert trimmed.is_file() and video.is_file()


def make_video(path: Path) -> None:
    run_process([
        discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
        "testsrc2=s=160x120:r=30:d=2", "-c:v", "libx264", str(path),
    ])


def test_preview_switches_from_still_back_to_source_video(tmp_path: Path) -> None:
    source = tmp_path / "mixed.mp4"
    run_process([
        discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
        "color=c=red:s=160x120:r=30:d=4,"
        "drawbox=x=0:y=0:w=iw:h=ih:color=blue:t=fill:enable='gte(t,2)'",
        "-c:v", "libx264", str(source),
    ])
    root = tmp_path / "project"
    project = create_project(root, [source])
    media = project.sources[0]
    make_proxies(root, project)
    plan = build_plan(project, [], 3).model_copy(update={"decisions": [
        RenderDecision(source_id=media.id, source_start_us=0, source_end_us=2_000_000,
                       action="HOLD", output_frame_count=30, provenance="user:verified"),
        RenderDecision(source_id=media.id, source_start_us=2_000_000,
                       source_end_us=media.duration_us, action="KEEP", output_frame_count=60,
                       provenance="user:keep"),
    ]})
    for preview in (False, True):
        result = render(root, project, plan, tmp_path / f"mixed-{preview}.mp4",
                        preview=preview, encoder="libx264")
        verify_output(result, 90, 30)
        capture = cv2.VideoCapture(str(result))
        try:
            capture.set(cv2.CAP_PROP_POS_FRAMES, 60)
            ok, frame = capture.read()
            assert ok
            # Preview letterboxes a 4:3 source; inspect pixels inside the image.
            height, width = frame.shape[:2]
            center = frame[height // 4:height * 3 // 4, width // 4:width * 3 // 4]
            assert center[:, :, 0].mean() > 200
            assert center[:, :, 2].mean() < 30
        finally:
            capture.release()


def test_cli_export_survives_missing_optional_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.mp4"
    make_video(source)
    root = tmp_path / "project"
    project = create_project(root, [source])
    plan = build_plan(project, [], 1)
    (root / "render_plan.json").write_text(plan.model_dump_json(), encoding="utf-8")

    def unavailable(*args: object) -> None:
        raise FileNotFoundError("missing review thumbnail")

    monkeypatch.setattr("sculptcut.cli.review_html", unavailable)
    result = root / "renders" / "checked.mp4"
    assert main(["render", str(root), "--output", str(result)]) == 0
    verify_output(result, 30, 30)
    assert read_variant(result)["project"] == project.model_dump(mode="json")


def test_gui_preserves_legacy_single_music_file_when_reopening_and_exporting(
    tmp_path: Path,
) -> None:
    app = QApplication.instance() or QApplication([])
    source = tmp_path / "source.mp4"
    make_video(source)
    music = tmp_path / "legacy.wav"
    run_process([
        discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
        "sine=frequency=440:duration=2", str(music),
    ])
    root = tmp_path / "project"
    project = create_project(root, [source]).model_copy(update={"settings": ProjectSettings(
        preset="speed", target_seconds=1, music_path=music, output_name="legacy",
    )})
    save_project(root, project)
    window = MainWindow()
    try:
        window.show()
        window.open_project_folder(root)
        assert window.music.text() == str(music)
        window.create_button.click()
        deadline = time.monotonic() + 30
        while window.job is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert window.job is None
        assert window.last_error == ""
        verify_output(root / "renders" / "legacy_speed.mp4", 30, 30, expected_audio=True)
        assert load_project(root).settings.music_path == music
        assert load_project(root).settings.music_folder is None
    finally:
        if window.job:
            window.job.cancel.set()
            window.job.wait(30_000)
            app.processEvents()
        window.close()
        app.processEvents()


def test_gui_remusic_uses_selected_variant_and_reports_its_job(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    source = tmp_path / "source.mp4"
    make_video(source)
    root = tmp_path / "project"
    project = create_project(root, [source])
    snapshot_project = project.model_copy(update={"settings": ProjectSettings(
        preset="shorts", target_seconds=2, music_fade_seconds=0, music_loop=False,
    )})
    current_project = project.model_copy(update={"settings": ProjectSettings(
        preset="speed", target_seconds=1, music_fade_seconds=1, music_loop=True,
    )})
    save_project(root, current_project)
    video = root / "renders" / "selected_shorts.mp4"
    make_video(video)
    before = fingerprint(video)
    save_variant(video, project=snapshot_project)
    folder = tmp_path / "music"
    folder.mkdir()
    run_process([
        discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
        "sine=frequency=440:duration=2", str(folder / "music.wav"),
    ])
    window = MainWindow()
    try:
        window.show()
        window.open_project_folder(root)
        window.music.setText(str(folder))
        window.job_succeeded = True  # Previous export must not leak into the new job.
        window.encoder_name = "old encoder"
        window.another_music()
        assert window.job is not None
        assert window.job_started_at is not None
        assert window.progress_timer.isActive()
        assert not window.job_succeeded
        assert window.encoder_name == ""
        assert not window.preset.isEnabled()
        deadline = time.monotonic() + 30
        while window.job is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert window.job is None
        assert window.last_error == ""
        result = window.last_result_video
        assert result is not None and result != video
        assert result.name.endswith("_shorts.mp4")
        verify_output(result, 60, 30, expected_audio=True)
        state = read_variant(result)
        assert state["project"]["settings"]["preset"] == "shorts"
        assert state["project"]["settings"]["music_fade_seconds"] == 0
        assert state["project"]["settings"]["music_loop"] is False
        assert load_project(root).settings.preset == "speed"
        assert window.progress_details.text().startswith("Завершено за")
        assert not window.progress_timer.isActive()
        assert window.preset.isEnabled()
        assert fingerprint(video) == before
    finally:
        if window.job:
            window.job.cancel.set()
            window.job.wait(30_000)
            app.processEvents()
        window.close()
        app.processEvents()


def test_saved_video_opens_when_next_music_settings_cannot_be_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    result = tmp_path / "renders" / "finished.mp4"
    result.parent.mkdir()
    result.touch()
    (tmp_path / "project.json").write_text(json.dumps([]), encoding="utf-8")
    opened: list[Path] = []
    monkeypatch.setattr(window, "open_video", lambda path, *args, **kwargs: opened.append(path))
    try:
        window.music.setText(str(tmp_path))
        window.show_result(str(result))
        assert opened == [result]
        assert window.job_succeeded
        assert "не сохранены" in window.status.text()
        assert result.is_file()
    finally:
        window.close()
        app.processEvents()
