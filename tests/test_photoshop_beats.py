import math
import shutil
import wave
from pathlib import Path
from threading import Event

import cv2
import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from sculptcut.domain import ProjectSettings, Roi
from sculptcut.media import Cancelled, discover, probe, run_process
from sculptcut.planning import plan_project
from sculptcut.project import create_project, fingerprint, load_project, save_project
from sculptcut.proxy import make_proxies
from sculptcut.remusic import replace_music
from sculptcut.rendering import render, verify_output
from sculptcut.ui import MainWindow


def _music(path: Path, interval: float) -> None:
    rate = 8000
    samples = np.zeros(rate * 6)
    for beat in np.arange(0.25, 5.7, interval):
        start, length = round(beat * rate), round(0.09 * rate)
        time = np.arange(length) / rate
        samples[start:start + length] = 0.8 * np.sin(2 * math.pi * 600 * time)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes((samples * 32767).astype("<i2").tobytes())


@pytest.mark.parametrize("layout", ["auto", "square"])
def test_photoshop_music_stills_keep_one_camera_in_real_export_and_preview(
    tmp_path: Path, layout: str,
) -> None:
    source = tmp_path / "drawing.mp4"
    run_process([
        discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
        "color=c=0x222222:s=320x180:r=15:d=8,"
        "drawbox=x=48:y=12:w=224:h=156:color=0xe0c080:t=fill,"
        "drawbox=x=125:y=35:w=70:h=105:color=0x553366:t=fill,"
        "drawbox=x=140:y=45:w=10:h=10:color=red:t=fill,"
        "drawbox=x=170:y=100:w=12:h=10:color=white:t=fill:enable='gte(t,2)',"
        "drawbox=x=165:y=120:w=22:h=10:color=white:t=fill:enable='gte(t,4)',"
        "drawbox=x=160:y=70:w=25:h=10:color=white:t=fill:enable='gte(t,6)'",
        "-c:v", "libx264", str(source),
    ])
    before = fingerprint(source)
    track = tmp_path / "music.wav"
    other = tmp_path / "other.wav"
    _music(track, 0.5)
    _music(other, 0.75)
    root = tmp_path / "project"
    project = create_project(root, [source])
    settings = ProjectSettings(
        preset="photoshop_beat_shorts", shorts_layout=layout, target_seconds=6,
        music_path=track, music_fade_seconds=0, beat_sync_strength=1,
        shorts_square_rois={project.sources[0].id: Roi(x=0.25, y=0.1, width=0.45, height=0.8)},
    )
    project = project.model_copy(update={
        "settings": settings,
        "rois": {project.sources[0].id: Roi(x=0.15, y=1/15, width=0.7, height=13/15)},
    })
    save_project(root, project)
    plan = plan_project(root, project, 6)
    shots = [d for d in plan.decisions if d.action != "DROP"]
    assert len(shots) >= 4
    assert all(d.action == "HOLD" for d in shots)
    assert all(d.subject_camera == shots[0].subject_camera for d in shots)
    assert shots[0].subject_camera is not None
    assert len(shots[0].subject_camera.points) == 1
    assert plan.settings["quiet_hold_zoom_max"] == 1
    assert plan.actual_frame_count == 180
    assert shots[-1].source_start_us > 7_000_000
    assert plan_project(root, project, 6) == plan
    updated = project.model_copy(update={"settings": settings.model_copy(
        update={"music_path": other},
    )})
    other_plan = plan_project(root, updated, 6)
    assert other_plan.settings["request_key"] != plan.settings["request_key"]
    assert [d.output_frame_count for d in other_plan.decisions if d.action == "HOLD"] != [
        d.output_frame_count for d in shots
    ]
    make_proxies(root, project)
    for preview in (False, True):
        output = render(root, project, plan, tmp_path / f"{preview}.mp4",
                        preview=preview, encoder="libx264")
        verify_output(output, 180, 30, expected_audio=True)
        metadata = probe(output)
        assert metadata.audio_streams
        assert (metadata.width, metadata.height) == ((360, 640) if preview else (1080, 1920))
        reader = cv2.VideoCapture(str(output))
        marker_centers = []
        count = 0
        try:
            while True:
                ok, frame = reader.read()
                if not ok:
                    break
                blue, green, red = cv2.split(frame)
                ys, xs = np.where((red > 160) & (green < 65) & (blue < 65))
                assert len(xs) > 4
                marker_centers.append((float(xs.mean()), float(ys.mean())))
                if layout == "auto":
                    body_y, body_x = np.where(
                        (blue > 80) & (green < 80) & (red > 50) & (red < 120)
                    )
                    assert max(
                        (body_y.max() - body_y.min()) / frame.shape[0],
                        (body_x.max() - body_x.min()) / frame.shape[1],
                    ) > 0.74
                    assert abs((body_x.max() + body_x.min()) / 2 / frame.shape[1] - 0.5) < 0.02
                if layout == "square":
                    margin = (frame.shape[0] - frame.shape[1]) // 2
                    # H.264 ringing/chroma subsampling can cross the square
                    # boundary. Check the solid band outside that codec edge.
                    assert np.max(np.abs(frame[:margin-16].astype(int) - 24)) <= 2
                    assert np.max(np.abs(frame[-margin+16:].astype(int) - 24)) <= 2
                count += 1
        finally:
            reader.release()
        assert count == 180
        # Musical cuts keep the fixed camera. Only the final hold has the requested zoom.
        from sculptcut.bookends import final_frames

        body = len(marker_centers)-final_frames(plan)
        assert np.max(np.ptp(np.array(marker_centers[:body]), axis=0)) < 2
    if layout == "auto":
        # Selecting an older musical export while editing a different preset
        # must rebuild that export's rhythm, keeping its camera and source ROIs.
        folder = tmp_path / "replacement-music"
        folder.mkdir()
        shutil.copy2(other, folder / "other.wav")
        save_project(root, project.model_copy(update={"settings": ProjectSettings(preset="speed")}))
        original_video = tmp_path / "False.mp4"
        original_fingerprint = fingerprint(original_video)
        replacement = replace_music(root, original_video, folder, 17, Event())
        verify_output(replacement, 180, 30, expected_audio=True)
        from sculptcut.variants import read_variant

        state = read_variant(replacement)
        assert state["operation"] == "music"
        assert state["project"]["settings"]["preset"] == "photoshop_beat_shorts"
        assert state["project"]["rois"] == project.model_dump(mode="json")["rois"]
        assert state["plan"]["settings"]["music_fingerprint"] != plan.settings["music_fingerprint"]
        assert state["plan"]["settings"]["request_key"] != plan.settings["request_key"]
        assert load_project(root).settings.preset == "speed"
        assert fingerprint(original_video) == original_fingerprint
    assert fingerprint(source) == before
    cancelled = Event()
    cancelled.set()
    with pytest.raises(Cancelled):
        plan_project(root, project, 6, cancelled)


def test_photoshop_music_preset_ui_and_required_music(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.show()
        window.preset.setCurrentIndex(window.preset.findData("photoshop_beat_shorts"))
        app.processEvents()
        assert window.shorts_mode.isChecked()
        assert window.shorts_layout.isVisible()
        assert not hasattr(window, "beat_sync")
        assert "пересобрать" in window.change_music_button.text()
        assert not hasattr(window, "square_button")
        window.folder.setText(str(tmp_path))
        window.set_sources([tmp_path / "source.mp4"])
        window.output_name.setText("drawing")
        window.create_button.click()
        assert window.job is None
        assert "папку с музыкой" in window.last_error
    finally:
        window.close()
        app.processEvents()
