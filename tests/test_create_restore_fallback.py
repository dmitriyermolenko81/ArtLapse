"""Old unreadable restore requests must not stop a fresh automatic Create."""
import json

import cv2
import numpy as np
import pytest
from PySide6.QtWidgets import QApplication
from test_creation_edits import wait_creation
from test_timeline import make_source

from sculptcut.analysis import extract_features
from sculptcut.domain import Override, Project, ProjectSettings
from sculptcut.motion import analyze_motion
from sculptcut.overrides import apply_overrides
from sculptcut.planning import RenderDecision, build_plan, plan_project
from sculptcut.project import create_project, save_project
from sculptcut.rendering import verify_output
from sculptcut.restored_work import NoReadableWork, find_work_windows, prepare_restored_work
from sculptcut.ui import MainWindow
from sculptcut.variants import read_variant


@pytest.mark.parametrize('preset', ['timelapse', 'shorts', 'music_shorts'])
def test_empty_restore_is_cached_without_resurrecting_or_dropping_the_whole_gap(tmp_path,
                                                                            monkeypatch, preset):
    old = Override(source_id='0', start_us=2_000_000, end_us=4_000_000,
                   action='SET_SPEED', speed=8, restore_omitted=True)
    cut = Override(source_id='0', start_us=1_000_000, end_us=2_000_000, action='DROP')
    project = Project(sources=[make_source(0, 0, 6_000_000)], overrides=[old, cut],
                      settings=ProjectSettings.model_validate({'preset': preset}))
    plan = build_plan(project, [], 2).model_copy(update={'decisions': [
        RenderDecision(source_id='0', source_start_us=a, source_end_us=b, action=action,
                       output_frame_count=count, provenance='story:work')
        for a, b, action, count in ((0, 1_000_000, 'SPEED', 30),
                                    (1_000_000, 5_000_000, 'DROP', 0),
                                    (5_000_000, 6_000_000, 'SPEED', 30))]})
    calls = []
    def unavailable(*args):
        calls.append(True)
        raise NoReadableWork('В выбранном пропуске нет читаемого участка работы для возврата.')
    monkeypatch.setattr('sculptcut.restored_work.find_work_windows', unavailable)
    prepared, updated, bounded = prepare_restored_work(tmp_path, project, plan)
    assert prepared.overrides == [cut] and not bounded
    assert project.overrides == [old, cut]  # Persisted user rules are untouched.
    assert json.loads(updated.settings['restored_work_selection'])['selections'] == [[]]
    again, _, bounded = prepare_restored_work(None, project, updated)
    assert again == prepared and not bounded and len(calls) == 1
    result = apply_overrides(prepared, updated, manual=True)
    assert result.actual_frame_count == 60
    assert all(not d.output_frame_count for d in result.decisions
               if d.source_start_us >= 1_000_000 and d.source_end_us <= 5_000_000)
    assert not any(o.start_us == old.start_us and o.end_us == old.end_us
                   for o in prepared.overrides)


def test_restore_infrastructure_failure_is_not_silenced(tmp_path, monkeypatch):
    project = Project(sources=[make_source(0, 0, 6_000_000)], overrides=[Override(
        source_id='0', start_us=0, end_us=6_000_000, action='SET_SPEED',
        restore_omitted=True)])
    def missing_analysis(*args):
        raise ValueError('Нужен анализ')
    monkeypatch.setattr('sculptcut.restored_work.find_work_windows', missing_analysis)
    with pytest.raises(ValueError, match='анализ'):
        prepare_restored_work(tmp_path, project, build_plan(project, [], 2))


def test_real_create_button_continues_after_an_empty_old_restore(tmp_path, monkeypatch):
    source = tmp_path/'work.mp4'
    writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*'mp4v'), 10, (320, 180))
    assert writer.isOpened()
    for i in range(360):
        image = np.full((180, 320, 3), 25, np.uint8)
        cv2.rectangle(image, (40, 20), (279, 159), (70, 70, 70), -1)
        cv2.ellipse(image, (160, 100), (30, 50), 0, 0, 360, (150, 150, 150), -1)
        if i >= 80:
            cv2.rectangle(image, (145, 70), (145+min(60, (i-80)//4), 125), (210, 210, 210), -1)
        writer.write(image)
    writer.release()
    original = source.read_bytes()
    root = tmp_path/'project'
    project = create_project(root, [source])
    sid = project.sources[0].id
    project = project.model_copy(update={'settings': ProjectSettings(target_seconds=3,
        proxy_width=320, proxy_fps=5, expressive_pacing=False, final_view_seconds=0),
        'overrides': [Override(source_id=sid, start_us=0, end_us=6_000_000,
            action='SET_SPEED', speed=8, restore_omitted=True),
            Override(source_id=sid, start_us=18_000_000, end_us=20_000_000, action='DROP')]})
    save_project(root, project)
    extract_features(root, project)
    analyze_motion(root, project)
    with pytest.raises(NoReadableWork):
        find_work_windows(root, project, project.overrides[0])
    plan = plan_project(root, project, 3)
    assert plan.actual_frame_count == 90
    assert json.loads(plan.settings['restored_work_selection'])['selections'] == [[]]
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    errors = []
    monkeypatch.setattr(window, 'show_error', errors.append)
    try:
        window.show()
        window.open_project_folder(root)
        window.output_name.setText('RestoreFallback')
        window.create_button.click()
        assert window.job is not None, errors
        wait_creation(window)
        assert not errors, errors
        output = window.last_result_video
        state = read_variant(output)
        assert state['plan']['actual_frame_count'] == 90
        assert all(d['source_end_us'] <= 18_000_000 or d['source_start_us'] >= 20_000_000
                   for d in state['plan']['decisions'] if d['output_frame_count'])
        assert not any(d['provenance'] == 'user:restored_work' for d in state['plan']['decisions'])
        verify_output(output, 90, 30, expected_audio=False)
        assert source.read_bytes() == original
    finally:
        window._allow_close = True
        window.close()
        app.processEvents()
