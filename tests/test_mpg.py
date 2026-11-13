"""MPG imports and elementary MPEG durations work for video bookends."""
from threading import Event

import cv2
import pytest
from PySide6.QtCore import QTimer, QUrl
from PySide6.QtWidgets import QApplication, QFileDialog
from test_episode_panel import wait_until

from sculptcut.domain import ProjectSettings
from sculptcut.media import Cancelled, discover, probe, run_process
from sculptcut.presentation import attach_presentation
from sculptcut.presentation_dialog import PresentationDialog
from sculptcut.project import create_project, fingerprint
from sculptcut.proxy import make_proxies
from sculptcut.rendering import verify_output
from sculptcut.source_cards import dropped_video_paths
from sculptcut.ui import MainWindow


@pytest.fixture
def mpg(tmp_path):
    clip = tmp_path/'ZBrush Movie.MPG'
    run_process([discover('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i',
        'color=c=red:s=320x180:r=30:d=2', '-vf',
        "drawbox=c=lime:t=fill:enable='gte(t,1)'", '-c:v', 'mpeg1video',
        '-f', 'mpeg1video', str(clip)])
    return clip


def test_raw_mpg_duration_fps_import_proxy_and_cancellation(mpg, tmp_path):
    info = probe(mpg)
    assert info.duration_us == 2_000_000 and (info.fps_num, info.fps_den) == (30, 1)
    assert dropped_video_paths([QUrl.fromLocalFile(str(mpg))]) == [mpg]
    root = tmp_path/'project'
    project = create_project(root, [mpg])
    assert project.sources[0].duration_us == 2_000_000
    make_proxies(root, project)
    cancel = Event()
    cancel.set()
    with pytest.raises(Cancelled):
        probe(mpg, cancel)


@pytest.mark.parametrize('position', ['start', 'end'])
def test_mpg_presentation_contains_entire_clip_at_either_end(mpg, tmp_path, position):
    body = tmp_path/'body.mp4'
    run_process([discover('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i',
                 'color=c=blue:s=320x180:r=30:d=1', '-c:v', 'libx264', str(body)])
    original = fingerprint(mpg)
    output = tmp_path/'presentation.mp4'
    settings = ProjectSettings(presentation_video=mpg, presentation_seconds=1,
                               presentation_position=position)
    attach_presentation(body, output, settings, width=320, height=180, fps=30, encoder='libx264')
    verify_output(output, 60, 30)
    reader = cv2.VideoCapture(str(output))
    try:
        start = 0 if position == 'start' else 30
        for index, channel in [(start, 2), (start+29, 1)]:
            reader.set(cv2.CAP_PROP_POS_FRAMES, index)
            ok, frame = reader.read()
            assert ok and frame[:, :, channel].mean() > 150
    finally:
        reader.release()
    assert fingerprint(mpg) == original


def test_mpg_file_picker_and_video_presentation_native_dialog(mpg, tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    filters = []

    def choose(*args, **kwargs):
        filters.append(args[3])
        return [str(mpg)], ''

    monkeypatch.setattr(QFileDialog, 'getOpenFileNames', choose)
    try:
        window.show()
        window.folder.setText(str(tmp_path/'project'))
        window.add_videos()
        assert '*.mpg' in filters[-1] and '*.mpeg' in filters[-1]
        errors = []

        def configure():
            dialog = window.findChild(PresentationDialog)
            try:
                assert dialog is not None
                dialog.video.setCurrentIndex(dialog.video.findData(str(mpg.resolve())))
                dialog.position.setCurrentIndex(1)
                dialog.seconds.setValue(2)
                dialog.accept()
            except Exception as exc:
                errors.append(str(exc))
                if dialog:
                    dialog.reject()

        QTimer.singleShot(50, configure)
        window.presentation_button.click()
        assert not errors
        assert window.presentation_video == mpg.resolve() and window.presentation_position == 'end'
        assert mpg.resolve() not in window.source_paths()  # Presentation is separate from work.
    finally:
        window._allow_close = True
        window.close()
        from sculptcut.source_cards import _active_source_loaders

        wait_until(app, lambda: not _active_source_loaders)
        app.processEvents()
