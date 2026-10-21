"""Single-file picking, cancellation and real GUI export/replacement of music."""

import time
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QFileDialog, QStyle, QStyleOptionButton
from test_app_checkup import make_video

from sculptcut.domain import ProjectSettings
from sculptcut.media import discover, run_process
from sculptcut.project import create_project, fingerprint, load_project, save_project
from sculptcut.rendering import verify_output
from sculptcut.ui import MainWindow
from sculptcut.ui_language import install_language
from sculptcut.variants import read_variant


@pytest.mark.parametrize("code", ["ru", "en"])
def test_file_picker_cancel_folder_and_clear_keep_one_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str,
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    track = tmp_path / "моя музыка.wav"
    track.write_bytes(b"picker test only")
    folder = tmp_path / "music"
    folder.mkdir()
    calls = []

    def select(*args):
        calls.append(args)
        return str(track), ""

    monkeypatch.setattr(QFileDialog, "getOpenFileName", select)
    try:
        window.change_language(code)
        window.resize(1120, 700)
        window.show()
        app.processEvents()
        button = window.music_file_button
        assert not button.icon().isNull()
        assert button.width() == button.height() == 36
        assert button.geometry().top() == window.music.geometry().top()
        assert button.height() == window.music.height()
        option = QStyleOptionButton()
        button.initStyleOption(option)
        content = button.style().subElementRect(
            QStyle.SubElement.SE_PushButtonContents, option, button,
        )
        assert content.width() >= button.iconSize().width()
        assert content.height() >= button.iconSize().height()
        assert button.parentWidget() == window.music.parentWidget()
        assert button.x() > window.music.geometry().right()
        # The gap and rounded button corners must show the panel, not a dark row rectangle.
        snapshot = window.grab().toImage()
        ratio = snapshot.devicePixelRatio()
        row = button.parentWidget()
        gap = row.mapTo(window, QPoint(button.x() - 3, button.y() + button.height() // 2))
        panel = window.settings_panel.mapTo(window, QPoint(2, window.settings_panel.height() // 2))
        corner = button.mapTo(window, QPoint(0, button.height() - 1))
        background = snapshot.pixelColor(int(panel.x() * ratio), int(panel.y() * ratio))
        for point in (gap, corner):
            assert snapshot.pixelColor(int(point.x() * ratio), int(point.y() * ratio)) == background

        assert button.parentWidget().rect().contains(button.geometry())
        assert button.accessibleName() == (
            "Выбрать музыкальный файл" if code == "ru" else "Choose an audio file"
        )
        button.click()
        assert window.music.text() == str(track)
        assert window.form_draft()["music"] == str(track)
        assert "*.wav" in calls[0][-1] and "*.flac" in calls[0][-1]
        previous = window.form_draft()
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: ("", ""))
        button.click()
        assert window.form_draft() == previous
        monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(folder))
        window.music.activated.emit()
        assert window.music.text() == str(folder)
        window.music.clear_action.trigger()
        assert window.music.text() == ""
    finally:
        window.close()
        install_language("ru")


def test_real_file_picker_save_export_and_change_music(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    source = tmp_path / "source.mp4"
    make_video(source)
    tracks = [tmp_path / "first.wav", tmp_path / "second.wav"]
    for track, frequency in zip(tracks, (440, 660), strict=True):
        run_process([
            discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
            f"sine=frequency={frequency}:duration=2", str(track),
        ])
    root = tmp_path / "project"
    project = create_project(root, [source]).model_copy(update={"settings": ProjectSettings(
        preset="speed", target_seconds=1, output_name="single", music_fade_seconds=0,
    )})
    save_project(root, project)
    window = MainWindow()

    def finish() -> None:
        deadline = time.monotonic() + 60
        while window.job is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.01)
        assert window.job is None
        assert window.last_error == ""

    try:
        window.show()
        window.open_project_folder(root)
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(tracks[0]), ""))
        window.music_file_button.click()
        window.save_current_project()
        finish()
        assert load_project(root).settings.music_path == tracks[0]
        assert load_project(root).settings.music_folder is None
        window.create_button.click()
        assert not window.music_file_button.isEnabled()
        finish()
        original = root / "renders/single_speed.mp4"
        verify_output(original, 30, 30, expected_audio=True)
        original_hash = fingerprint(original)
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(tracks[1]), ""))
        window.music_file_button.click()
        # A valid selected file must bypass the folder picker during replacement.
        monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: pytest.fail(
            "Single-file music must not open the folder picker",
        ))
        window.change_music_button.click()
        finish()
        output = window.last_result_video
        assert output is not None and output != original
        verify_output(output, 30, 30, expected_audio=True)
        run_process([discover("ffmpeg"), "-v", "error", "-xerror", "-i", str(output),
                     "-f", "null", "-"])
        assert load_project(root).settings.music_path == tracks[1]
        assert load_project(root).settings.music_folder is None
        assert read_variant(output)["project"]["settings"]["music_path"] == str(tracks[1])
        pcm = tmp_path / "replaced-audio.pcm"
        run_process([
            discover("ffmpeg"), "-v", "error", "-i", str(output), "-vn", "-ac", "1",
            "-ar", "8000", "-f", "s16le", str(pcm),
        ])
        samples = np.frombuffer(pcm.read_bytes(), dtype="<i2")
        frequencies = np.fft.rfftfreq(len(samples), 1 / 8000)
        dominant = frequencies[np.argmax(np.abs(np.fft.rfft(samples)))]
        assert abs(dominant - 660) < 5  # The second selected track, not the original 440 Hz.
        assert fingerprint(original) == original_hash
    finally:
        if window.job:
            window.job.cancel.set()
            window.job.wait(30_000)
            app.processEvents()
        window.close()
        app.processEvents()
