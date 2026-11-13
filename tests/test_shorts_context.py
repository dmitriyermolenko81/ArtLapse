"""Raw Shorts windows and final exports share portrait/square/camera framing."""
from threading import Event

import cv2
import numpy as np
import pytest
from test_episode_roll import roll_movie as roll_movie

from sculptcut.domain import CameraPoint, Roi, SubjectCamera
from sculptcut.episode_context_preview import PreviewScene, prepare_clip, shorts_scenes
from sculptcut.media import probe
from sculptcut.planning import build_plan
from sculptcut.rendering import render, verify_output
from sculptcut.shorts_layout import apply_square_layout


@pytest.mark.parametrize('composition', ['blur', 'square', 'camera'])
def test_short_context_matches_export_geometry_and_pixels(roll_movie, composition):
    root, project, plan, _, video = roll_movie
    project = project.model_copy(update={'settings': project.settings.model_copy(update={
        'preset': 'music_shorts', 'intro_image': None, 'music_path': None,
        'target_seconds': 1, 'shorts_layout': 'square' if composition == 'square' else 'auto',
        'shorts_square_rois': {project.sources[0].id: Roi(x=.25, y=.1, width=.5, height=.8)}})})
    plan = plan.model_copy(update={'settings': build_plan(project, [], 1).settings,
        'decisions': [d if i == 0 else d.model_copy(update={
            'action': 'DROP', 'output_frame_count': 0})
                      for i, d in enumerate(plan.decisions)],
        'target_frame_count': 30, 'actual_frame_count': 30})
    if composition == 'square':
        plan = apply_square_layout(project, plan)
    if composition == 'camera':
        camera = SubjectCamera(viewport=Roi(x=0,y=0,width=1,height=1),
            width=.5, height=.8, background=24, fit_subject=True,
            points=[CameraPoint(time_us=0, x=.5, y=.5)])
        plan = plan.model_copy(update={'decisions': [plan.decisions[0].model_copy(
            update={'subject_camera': camera}), *plan.decisions[1:]]})
    d = plan.decisions[0]
    scenes = (PreviewScene(0, project.sources[0].path, 0, 3000, 30, d),)
    prepared = shorts_scenes(scenes, project, (360, 640), 30)
    scratch = root/'context'
    scratch.mkdir()
    clip = prepare_clip(scratch, prepared, 30, (360, 640), Event())
    verify_output(clip.path, 30, 30)
    info = probe(clip.path)
    assert (info.width, info.height) == (360, 640)
    output = root/'renders/framed.mp4'
    render(root, project, plan, output, encoder='libx264')
    def frame(path):
        capture = cv2.VideoCapture(str(path))
        ok, image = capture.read()
        capture.release()
        assert ok
        return cv2.resize(image, (360, 640)).astype(float)
    small, exported = frame(clip.path), frame(output)
    assert np.mean(np.abs(small-exported)) < 10
    if composition == 'square':
        assert np.max(np.abs(small[:100].mean(axis=(0,1))-24)) < 4
    # Reused saved scenes must not receive a second crop or camera transform.
    saved = (PreviewScene(0, video, 0, 1000, 30),)
    assert shorts_scenes(saved, project, (360, 640), 30) == saved
