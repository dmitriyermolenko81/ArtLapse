import wave
from pathlib import Path
from threading import Event

import numpy as np
import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from test_timeline import make_source

from sculptcut.domain import Project, ProjectSettings
from sculptcut.media import discover, probe, run_process
from sculptcut.output_cut_editor import OutputCutEditor
from sculptcut.output_cuts import cut_output, retained_ranges
from sculptcut.project import fingerprint
from sculptcut.ui import MainWindow
from sculptcut.variants import read_variant, save_variant


def test_compressed_timeline_cuts_undo_and_zoom() -> None:
    app = QApplication.instance() or QApplication([])
    editor = OutputCutEditor()
    editor.resize(815, 150)
    editor.set_duration(10_000)
    editor.show()
    app.processEvents()
    try:
        editor.start.setValue(2)
        editor.end.setValue(4)
        editor.add_cut()
        assert editor.edited_duration == 8_000_000
        assert editor.to_source(2000) == 4000
        assert editor.to_edited(3000) == 2000
        editor.add_cut()  # No selection: never repeat the old cut.
        assert len(editor.cuts) == 1
        editor.start.setValue(1)
        editor.end.setValue(3)  # Crosses the newly created join.
        editor.add_cut()
        assert editor.ranges == [(0, 1_000_000), (5_000_000, 10_000_000)]
        assert editor.edited_duration == 6_000_000
        editor.undo()
        assert editor.edited_duration == 8_000_000
        bar = editor.timeline
        x = bar.x_at(4)
        event = QWheelEvent(
            QPointF(x, 15),
            QPointF(x, 15),
            QPoint(),
            QPoint(0, 120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        QApplication.sendEvent(bar, event)
        assert bar.span < 8
        assert abs(bar.time_at(x) - 4) < 0.001
        QTest.mouseDClick(bar, Qt.MouseButton.LeftButton, pos=QPoint(400, 40))
        assert (bar.view_start, bar.span) == (0, 8)
        editor.undo()
        assert editor.edited_duration == 10_000_000
        assert not editor.cuts
    finally:
        editor.close()
        app.processEvents()


def test_player_uses_edited_time_and_skips_cut(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    source = tmp_path / "renders" / "movie.mp4"
    source.parent.mkdir()
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=160x120:r=30:d=4",
            "-c:v",
            "libx264",
            str(source),
        ]
    )
    window = MainWindow()
    window.folder.setText(str(tmp_path))
    window.show()
    window.toggle_output_editor()
    try:
        for _ in range(100):
            if window.player.duration() == 4000:
                break
            QTest.qWait(20)
        assert window.player.duration() == 4000
        editor = window.output_editor
        assert window.cut_button.isChecked()
        assert window.cut_button.parentWidget() is window.cut_section
        assert editor.parentWidget() is window.cut_section
        assert not window.cut_section.isAncestorOf(window.play_button)
        assert not window.cut_section.isAncestorOf(window.volume_control)
        assert window.cut_button.text() == "Подрезка ролика"
        assert "Скрыть" in window.cut_button.toolTip()
        assert not window.cut_button.icon().isNull()
        QTest.qWait(100)
        editor_height = window.height()
        window.toggle_output_editor()
        assert not window.cut_button.isChecked()
        assert window.cut_button.text() == "Подрезка ролика"
        assert "Открыть" in window.cut_button.toolTip()
        assert not window.cut_button.icon().isNull()
        window.toggle_output_editor()
        assert window.cut_button.isChecked()
        QTest.qWait(100)
        assert window.height() == editor_height
        editor.start.setValue(1)
        editor.end.setValue(3)
        editor.add_cut()
        assert window.seek.maximum() == 2000
        assert window.video_time.text().endswith("00:02")
        previous = list(editor.cuts)
        QTest.mouseClick(window.cut_button, Qt.MouseButton.LeftButton)
        assert not editor.isVisible() and not window.cut_button.isChecked()
        for _ in range(20):
            if window.cut_section.height() <= window.cut_button.height() + 2:
                break
            QTest.qWait(10)
        assert window.cut_section.height() <= window.cut_button.height() + 2
        assert editor.cuts == previous
        QTest.mouseClick(window.cut_button, Qt.MouseButton.LeftButton)
        assert editor.isVisible() and window.cut_button.isChecked()
        assert editor.cuts == previous
        assert window.seek.maximum() == 2000

        window.seek_preview(1500)
        assert window.player.position() == 3500
        assert window.seek.source_time(1500) == 3500
        QTest.qWait(300)  # Allow the resized layout and paused decoded frame to settle.
        bar = editor.timeline
        hover_point = QPoint(round(bar.x_at(1.5)), 15)
        QApplication.sendEvent(
            bar,
            QMouseEvent(
                QEvent.Type.MouseMove,
                QPointF(hover_point),
                QPointF(bar.mapToGlobal(hover_point)),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            ),
        )
        app.processEvents()
        assert window.seek.popup.isVisible()
        assert abs(window.seek.hover_ms - 1500) < 20
        assert abs(window.seek.source_time(window.seek.hover_ms) - 3500) < 20
        for _ in range(100):
            if not window.seek.picture.pixmap().isNull():
                break
            QTest.qWait(30)
        assert not window.seek.picture.pixmap().isNull()
        assert editor.summary.geometry().top() >= bar.geometry().bottom()
        assert editor.height() >= editor.minimumSizeHint().height()
        window.seek.hide_hover()
        window.update_position(1500)  # Playback entered a removed source interval.
        assert window.player.position() == 3000
        editor.undo()
        assert window.seek.maximum() <= 4000
        assert editor.edited_duration == 4_000_000
        window.grab().save(str(tmp_path / "edited-player.png"))
    finally:
        window.close()
        app.processEvents()


def test_merge_and_all_removed() -> None:
    assert retained_ranges(100, [(20, 40), (30, 50), (50, 60)]) == [(0, 20), (60, 100)]
    with pytest.raises(ValueError):
        retained_ranges(100, [(0, 100)])


@pytest.mark.parametrize("audio", [False, True])
def test_real_cut_preserves_original_and_sound(tmp_path: Path, audio: bool) -> None:
    source = tmp_path / "original.mp4"
    args = [discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x120:r=30:d=4"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-c:a", "aac"]
    run_process(args + ["-c:v", "libx264", str(source)])
    original = fingerprint(source)
    existing = source.with_name("original-edit-001.mp4")
    existing.write_bytes(b"keep")
    result = cut_output(source, [(1_000_000, 3_000_000)], Event(), lambda p: None)
    info = probe(result)
    assert info.duration_us is not None and abs(info.duration_us - 2_000_000) < 250_000
    assert bool(info.audio_streams) == audio
    assert (info.width, info.height) == (160, 120)
    assert fingerprint(source) == original
    assert existing.read_bytes() == b"keep"
    assert result.name == "original-edit-002.mp4"


def test_cut_rebuilds_saved_music_for_the_new_timeline(tmp_path: Path) -> None:
    music = tmp_path / "music.wav"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=2",
            "-filter_complex",
            "[0:a][1:a]concat=n=2:v=0:a=1[out]",
            "-map",
            "[out]",
            str(music),
        ]
    )
    source = tmp_path / "model_timelapse.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=160x120:r=30:d=4",
            "-i",
            str(music),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-shortest",
            str(source),
        ]
    )
    project = Project(
        sources=[make_source(0, 0, 4_000_000)],
        settings=ProjectSettings(
            music_path=music,
            music_loop=False,
            music_fade_seconds=0,
        ),
    )
    save_variant(source, project=project, music=music)

    result = cut_output(source, [(500_000, 2_500_000)], Event(), lambda _value: None)

    info = probe(result)
    assert info.duration_us is not None and abs(info.duration_us - 2_000_000) < 250_000
    assert info.audio_streams
    sample = tmp_path / "sample.wav"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-ss",
            "0.9",
            "-i",
            str(result),
            "-t",
            "0.4",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-c:a",
            "pcm_s16le",
            str(sample),
        ]
    )
    with wave.open(str(sample), "rb") as stream:
        samples = np.frombuffer(stream.readframes(stream.getnframes()), dtype=np.int16)
        rate = stream.getframerate()
    spectrum = np.abs(np.fft.rfft(samples.astype(np.float64)))
    dominant = np.fft.rfftfreq(len(samples), 1 / rate)[int(np.argmax(spectrum[1:])) + 1]
    assert 420 < dominant < 460  # The new timeline restarts music instead of jumping to 880 Hz.
    state = read_variant(result)
    assert state["operation"] == "cut"
    assert state["music"]


def test_editor_marks_and_undo() -> None:
    app = QApplication.instance() or QApplication([])
    editor = OutputCutEditor()
    editor.set_duration(4000)
    assert not editor.remove_button.isEnabled()
    assert not editor.undo_button.isEnabled()
    editor.start.setValue(1)
    editor.end.setValue(3)
    assert editor.remove_button.isEnabled()
    editor.add_cut()
    assert editor.cuts == [(1_000_000, 3_000_000)]
    assert editor.save_button.isEnabled()
    assert editor.undo_button.isEnabled()
    editor.undo()
    assert not editor.cuts and not editor.save_button.isEnabled()
    assert not editor.undo_button.isEnabled()
    editor.close()
    app.processEvents()


def test_visual_selection_handles(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    editor = OutputCutEditor()
    editor.resize(815, 150)
    editor.set_duration(10_000)
    editor.show()
    app.processEvents()
    bar = editor.timeline
    seeks: list[int] = []
    bar.seek_requested.connect(seeks.append)

    def point(seconds: float) -> QPoint:
        return QPoint(round(bar.x_at(seconds)), 16)

    try:
        QTest.mousePress(bar, Qt.MouseButton.LeftButton, pos=point(2))
        QTest.mouseMove(bar, point(6))
        QTest.mouseRelease(bar, Qt.MouseButton.LeftButton, pos=point(6))
        assert abs(editor.start.value() - 2) < 0.02
        assert abs(editor.end.value() - 6) < 0.02
        QTest.mousePress(bar, Qt.MouseButton.LeftButton, pos=point(6))
        QTest.mouseMove(bar, point(7))
        QTest.mouseRelease(bar, Qt.MouseButton.LeftButton, pos=point(7))
        assert abs(editor.end.value() - 7) < 0.02
        assert abs(seeks[-1] - 7000) < 20
        editor.add_cut()
        assert len(editor.cuts) == 1
        editor.grab().save(str(tmp_path / "visual-cuts.png"))
    finally:
        editor.close()
        app.processEvents()
