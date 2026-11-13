"""New results start on the retained opening, and bookend scrubbing stays on MP4 time."""
from pathlib import Path

import cv2
import pytest
from PySide6.QtWidgets import QApplication
from test_cursor_playback import finish
from test_episode_panel import wait_until
from test_episode_roll import roll_movie as roll_movie

from sculptcut.intro import output_frame_count
from sculptcut.planning import build_plan
from sculptcut.project import save_project
from sculptcut.rendering import render, verify_output
from sculptcut.ui import MainWindow


@pytest.mark.parametrize('opening', ['cover', 'presentation', 'both', 'shorts'])
def test_new_result_handoff_starts_at_zero_with_real_opening(roll_movie, opening):
    root, project, plan, _, old_video = roll_movie
    settings = project.settings.model_copy(update={
        'preset': 'music_shorts' if opening == 'shorts' else 'timelapse',
        'intro_position': 'start' if opening != 'presentation' else 'end',
        'presentation_video': project.sources[0].path if opening != 'cover' else None,
        'presentation_seconds': 1, 'presentation_position': 'start',
        'presentation_source_index': 0,
    })
    project = project.model_copy(update={'settings': settings})
    plan = plan.model_copy(update={'settings': build_plan(project, [], 4.5).settings})
    save_project(root, project)
    video = old_video.with_name(f'{opening}.mp4')
    render(root, project, plan, video, encoder='libx264')
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    window.folder.setText(str(root))
    window.preset.setCurrentIndex(window.preset.findData(settings.preset))
    dialog = None
    try:
        window.show_result(str(video))
        wait_until(app, lambda: window.selection_dialog is not None
                   and not window.selection_dialog.loading)
        dialog = window.selection_dialog
        wait_until(app, lambda: dialog.sequence.enabled and dialog.playback.is_advancing(), 20)
        assert dialog.sequence.initial_cursor == 0
        assert dialog.sequence.entries[0][0] == 0
        assert dialog.sequence.entries[0][2].path == video
        assert dialog.sequence.cursor < dialog.montage_seek.blocks[0][1]
        window.play_button.click()
        original = dialog.session.plan
        frames = []
        dialog.scrub_frames.ready.connect(
            lambda key, image: frames.append(image) if key[1] == 0 else None)
        dialog.seek_montage(0)
        wait_until(app, lambda: bool(frames))
        assert dialog.montage_seek.value() == 0 and dialog.saved_scrub
        assert dialog.scrub_frames.wanted[0] == video.resolve()
        assert dialog.scrub_frames.wanted[1] == 0
        assert dialog.session.plan == original
        capture = cv2.VideoCapture(str(video))
        try:
            ok, first = capture.read()
            assert ok
        finally:
            capture.release()
        expected = tuple(int(v) for v in first[first.shape[0]//2, first.shape[1]//2][::-1])
        image = frames[-1]
        pixel = image.pixelColor(image.width()//2, image.height()//2)
        assert max(abs(a-b) for a, b in zip(expected, pixel.getRgb()[:3], strict=True)) < 15
        # Footer and prefix are both sought in the saved video, never the first/last work scene.
        dialog.seek_montage(dialog.montage_seek.maximum())
        assert dialog.saved_scrub and dialog.scrub_frames.wanted[0] == video.resolve()
        folder = Path('artifacts/playback-start-325')
        folder.mkdir(exist_ok=True, parents=True)
        dialog.seek_montage(0)
        wait_until(app, lambda: not dialog.frame_image.isNull())
        assert window.grab().save(str(folder/f'{opening}.png'))
        verify_output(video, output_frame_count(settings, plan.output_fps,
            plan.actual_frame_count), plan.output_fps, expected_audio=True)
    finally:
        if dialog is not None:
            finish(window, dialog, app)
        else:
            window._allow_close = True
            window.close()
