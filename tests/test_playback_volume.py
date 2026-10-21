"""Real preview playback, shared loudness, persistence and export isolation."""

from pathlib import Path
from time import monotonic, sleep

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QSettings, Qt, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from sculptcut.domain import ProjectSettings
from sculptcut.episode_panel import EpisodePanel
from sculptcut.media import discover, run_process
from sculptcut.planning import build_plan
from sculptcut.playback_volume import PlaybackVolume
from sculptcut.project import create_project, fingerprint, save_project
from sculptcut.ui import MainWindow


def wait_for(app: QApplication, predicate) -> None:
    deadline = monotonic() + 12
    while not predicate() and monotonic() < deadline:
        app.processEvents()
        sleep(.01)
    assert predicate()


def test_real_video_volume_sync_mute_reopen_and_project_unchanged(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    source = tmp_path / "audio-video.mp4"
    run_process([
        discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
        "testsrc2=s=160x120:r=30:d=4", "-f", "lavfi", "-i",
        "sine=frequency=440:duration=4", "-c:v", "libx264", "-c:a", "aac", str(source),
    ])
    original = fingerprint(source)
    root = tmp_path / "project"
    project = create_project(root, [source]).model_copy(update={"settings": ProjectSettings(
        preset="speed", target_seconds=4,
    )})
    save_project(root, project)
    plan = build_plan(project, [], 4)
    plan_path = root / "render_plan.json"
    plan_path.write_text(plan.model_dump_json(), encoding="utf-8")
    before = (root / "project.json").read_bytes(), plan_path.read_bytes()
    window = MainWindow()
    panel = None
    try:
        window.resize(1120, 700)
        window.show()
        window.player.setSource(QUrl.fromLocalFile(str(source)))
        window.player.play()
        wait_for(app, lambda: window.video_frames_seen > 0 and window.player.position() > 100)
        control = window.volume_control
        control.slider.setValue(27)
        assert window.audio_output.volume() == pytest.approx(.27)
        QTest.mouseClick(control.mute_button, Qt.MouseButton.LeftButton)
        assert window.audio_output.isMuted() and control.slider.value() == 27
        position = window.player.position()
        wait_for(app, lambda: window.player.position() > position + 100)
        QTest.mouseClick(control.mute_button, Qt.MouseButton.LeftButton)
        assert not window.audio_output.isMuted()
        assert window.audio_output.volume() == pytest.approx(.27)
        window.player.pause()
        panel = EpisodePanel(root, window, volume_state=window.volume_state)
        panel.show()
        panel.playback.select(source, 0, 4000, 0)
        panel.playback.play()
        wait_for(app, lambda: panel.playback.player.position() > 100)
        assert panel.playback.audio.volume() == pytest.approx(.27)
        panel.volume_control.slider.setValue(0)
        assert panel.playback.audio.isMuted() and window.audio_output.isMuted()
        assert control.slider.value() == 0
        panel.volume_control.mute_button.click()
        assert panel.volume_control.slider.value() == control.slider.value() == 27
        assert not panel.playback.audio.isMuted()
        panel.volume_control.slider.setValue(64)
        window.change_language("en")
        assert control.mute_button.toolTip() == "Mute"
        assert panel.volume_control.slider.accessibleName() == "Playback volume"
        assert "64%" in control.slider.toolTip()
        panel.volume_control.mute_button.click()
        panel.reject()
        panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        panel = None
        # Closing an episode output must disconnect its bindings from the shared state.
        control.mute_button.click()
        assert not window.audio_output.isMuted()
        control.mute_button.click()
        assert (root / "project.json").read_bytes() == before[0]
        assert plan_path.read_bytes() == before[1]
        assert fingerprint(source) == original
        window.close()
        restored = MainWindow()
        try:
            assert restored.volume_control.slider.value() == 64
            assert restored.audio_output.volume() == pytest.approx(.64)
            assert restored.audio_output.isMuted()
            restored.volume_control.mute_button.click()
            assert not restored.audio_output.isMuted()
            assert restored.volume_control.slider.value() == 64
        finally:
            restored.close()
    finally:
        if panel is not None:
            panel.reject()
        window.close()
        app.processEvents()


@pytest.mark.parametrize("stored, expected", [("broken", 50), (-9, 0), (250, 100)])
def test_invalid_saved_volume_is_safe(tmp_path: Path, stored: object, expected: int) -> None:
    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "volume.ini"), QSettings.Format.IniFormat)
    settings.setValue("playback_volume", stored)
    state = PlaybackVolume(settings)
    assert state.level == expected
    assert state.silent == (expected == 0)
    app.processEvents()
