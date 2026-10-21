from pathlib import Path
from time import monotonic, sleep

import cv2
import numpy as np
import pytest
from PySide6.QtCore import QPoint, Qt, QThread, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from sculptcut.crop_dialog import CropCanvas, TimelapseCropDialog
from sculptcut.domain import ProjectSettings, Roi
from sculptcut.intro import output_frame_count
from sculptcut.media import discover, probe, run_process
from sculptcut.planning import build_plan
from sculptcut.project import create_project, fingerprint, load_project, save_project
from sculptcut.proxy import make_proxies
from sculptcut.rendering import render, verify_output
from sculptcut.shorts_layout import apply_square_layout
from sculptcut.ui import Job, MainWindow


def test_square_selection_and_project_restore(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    frame = QPixmap(400, 200)
    frame.fill(Qt.GlobalColor.gray)
    canvas = CropCanvas(frame, Roi(x=0, y=0, width=1, height=1), aspect=1)
    window = MainWindow()
    try:
        canvas.show()
        app.processEvents()
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseMove(canvas, QPoint(180, 180))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(180, 180))
        assert canvas.roi.width * 400 == pytest.approx(canvas.roi.height * 200)
        assert canvas.roi.x == pytest.approx(0.05)
        window.show()
        window.preset.setCurrentIndex(window.preset.findData("shorts"))
        window.shorts_layout.setCurrentIndex(window.shorts_layout.findData("square"))
        app.processEvents()
        assert window.shorts_layout.isVisible()
        assert not hasattr(window, "square_button")
        assert not hasattr(window, "shorts_crop_note")
        settings = ProjectSettings(shorts_layout="square", shorts_square_rois={"one": canvas.roi})
        assert ProjectSettings.model_validate_json(settings.model_dump_json()) == settings
        window.preset.setCurrentIndex(window.preset.findData("timelapse"))
        assert not window.shorts_layout.isVisible()
    finally:
        canvas.close()
        window.close()
        app.processEvents()


@pytest.mark.parametrize("preset", [
    "shorts", "beat_shorts", "photoshop_shorts", "photoshop_beat_shorts",
])
def test_create_opens_square_dialog_and_remembers_selection(
    tmp_path: Path, preset: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    source = tmp_path / "source.mp4"
    run_process([
        discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
        "color=c=gray:s=320x180:r=15:d=1", "-c:v", "libx264", str(source),
    ])
    root = tmp_path / "project"
    project = create_project(root, [source])
    previous = Roi(x=0.1, y=0.1, width=0.45, height=0.8)
    selected = Roi(x=0.3, y=0.1, width=0.45, height=0.8)
    settings = ProjectSettings(
        preset=preset, shorts_layout="square",
        shorts_square_rois={project.sources[0].id: previous},
    )
    project = project.model_copy(update={"settings": settings})
    save_project(root, project)
    window = MainWindow()
    launches: list[Job] = []

    def start(job: Job) -> None:
        if job.objectName() == "preflight":
            QThread.start(job)
        else:
            launches.append(job)

    def wait_preflight() -> None:
        deadline = monotonic() + 20
        while window.job and window.job.objectName() == "preflight" and monotonic() < deadline:
            app.processEvents()
            sleep(0.01)
        assert not window.last_error

    monkeypatch.setattr(Job, "start", start)
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 "sine=frequency=440:duration=2", str(tmp_path / "music.wav")])
    timer = QTimer()
    opened: list[Roi] = []
    accept = False

    def handle_dialog() -> None:
        dialog = app.activeModalWidget()
        if isinstance(dialog, TimelapseCropDialog) and dialog.canvas is not None:
            opened.append(dialog.initial_roi)
            dialog.canvas.roi = selected
            if accept:
                dialog.accept()
            else:
                dialog.reject()

    timer.timeout.connect(handle_dialog)
    timer.start(10)
    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(lambda: app.activeModalWidget().close()
                             if app.activeModalWidget() is not None else None)
    try:
        window.folder.setText(str(root))
        window.set_sources([source])
        window.preset.setCurrentIndex(window.preset.findData(preset))
        window.restore_square_layout(load_project(root))
        window.music.setText(str(tmp_path))
        window.output_name.setText("square")
        window.show()
        app.processEvents()
        watchdog.start(10_000)
        window.create_button.click()
        wait_preflight()
        assert opened == [previous]
        assert window.job is None
        assert launches == []
        assert window.square_rois[source.resolve()] == previous
        accept = True
        watchdog.start(10_000)
        window.create_button.click()
        wait_preflight()
        assert opened == [previous, previous]
        assert len(launches) == 1
        assert window.square_rois[source.resolve()] == selected
        # The pipeline persists this same per-source selection; a reopened
        # project must seed the next dialog with it.
        save_project(root, project.model_copy(update={"settings": settings.model_copy(
            update={"shorts_square_rois": {project.sources[0].id: selected}},
        )}))
        window.job = None
        window.job_finished()
        window.restore_square_layout(load_project(root))
        accept = False
        watchdog.start(10_000)
        window.create_button.click()
        wait_preflight()
        assert opened == [previous, previous, selected]
        assert len(launches) == 1
        assert window.square_rois[source.resolve()] == selected
    finally:
        timer.stop()
        watchdog.stop()
        window.job = None
        window.close()
        app.processEvents()


@pytest.mark.parametrize("preset", ["shorts", "beat_shorts", "photoshop_shorts"])
def test_square_real_export_and_preview(tmp_path: Path, preset: str) -> None:
    source = tmp_path / "source.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=320x180:r=30:d=2,drawbox=x=70:y=0:w=180:h=180:color=lime:t=fill",
            "-c:v",
            "libx264",
            str(source),
        ]
    )
    before = fingerprint(source)
    root = tmp_path / "project"
    created = create_project(root, [source])
    source_id = created.sources[0].id
    settings = ProjectSettings.model_validate(
        {
            "preset": preset,
            "shorts_layout": "square",
            "target_seconds": 1,
            "shorts_square_rois": {source_id: Roi(x=70 / 320, y=0, width=180 / 320, height=1)},
        }
    )
    project = created.model_copy(update={"settings": settings})
    save_project(root, project)
    restored = load_project(root)
    assert restored.settings.shorts_square_rois == settings.shorts_square_rois
    raw = build_plan(project, [], 1)
    missing = project.model_copy(
        update={
            "settings": settings.model_copy(update={"shorts_square_rois": {}}),
        }
    )
    with pytest.raises(ValueError, match="Set a square crop"):
        apply_square_layout(missing, raw)
    automatic = project.model_copy(
        update={
            "settings": settings.model_copy(update={"shorts_layout": "auto"}),
        }
    )
    assert apply_square_layout(automatic, raw) is raw
    if preset == "beat_shorts":
        raw = raw.model_copy(
            update={
                "decisions": [
                    d.model_copy(update={"action": "HOLD", "provenance": "auto:beat_still"})
                    for d in raw.decisions
                ],
                "settings": raw.settings | {"quiet_hold_zoom_max": 1.04},
            }
        )
    plan = apply_square_layout(project, raw)
    assert not plan.settings["bookends"]
    assert all(d.subject_camera is None for d in plan.decisions)
    make_proxies(root, project)
    for preview in (False, True):
        output = render(
            root,
            project,
            plan,
            tmp_path / f"square-{preview}.mp4",
            encoder="libx264",
            preview=preview,
        )
        verify_output(output, 30, 30)
        info = probe(output)
        assert (info.width, info.height) == ((360, 640) if preview else (1080, 1920))
        run_process(
            [discover("ffmpeg"), "-v", "error", "-xerror", "-i", str(output), "-f", "null", "-"]
        )
        capture = cv2.VideoCapture(str(output))
        try:
            for index in (0, 15, 29):
                capture.set(cv2.CAP_PROP_POS_FRAMES, index)
                ok, image = capture.read()
                assert ok
                scale = info.width / 1080
                assert (
                    np.max(np.abs(image[round(20 * scale) : round(400 * scale)].astype(int) - 24))
                    <= 4
                )
                assert (
                    np.max(
                        np.abs(image[round(1520 * scale) : round(1900 * scale)].astype(int) - 24)
                    )
                    <= 4
                )
                # The exact source square fills the center, without side pillars or red UI.
                for y in (450, 960, 1470):
                    for x in (30, 540, 1050):
                        assert image[round(y * scale), round(x * scale), 1] > 220
                        assert image[round(y * scale), round(x * scale), 2] < 20
        finally:
            capture.release()
    assert fingerprint(source) == before

    if preset == "beat_shorts":
        cover = tmp_path / "cover.png"
        cv2.imencode(".png", np.full((80, 120, 3), (255, 0, 0), dtype=np.uint8))[1].tofile(cover)
        intro_project = project.model_copy(
            update={
                "settings": settings.model_copy(update={"intro_image": cover}),
            }
        )
        intro_plan = apply_square_layout(intro_project, build_plan(intro_project, [], 1))
        output = render(
            root, intro_project, intro_plan, tmp_path / "square-intro.mp4", encoder="libx264"
        )
        expected = output_frame_count(intro_project.settings, 30, 30)
        verify_output(output, expected, 30)
        run_process(
            [discover("ffmpeg"), "-v", "error", "-xerror", "-i", str(output), "-f", "null", "-"]
        )
        capture = cv2.VideoCapture(str(output))
        try:
            for index in (0, 50, expected - 1):
                capture.set(cv2.CAP_PROP_POS_FRAMES, index)
                ok, image = capture.read()
                assert ok
                assert np.max(np.abs(image[20:400].astype(int) - 24)) <= 4
                assert np.max(np.abs(image[1520:1900].astype(int) - 24)) <= 4
        finally:
            capture.release()
