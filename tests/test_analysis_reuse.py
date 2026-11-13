"""Real video analysis is reused, invalidated and cancelled at the correct layer."""
from dataclasses import replace
from threading import Event

import pytest

import sculptcut.activity as activity
import sculptcut.importance as importance
from sculptcut.analysis import extract_features, read_features
from sculptcut.domain import Override, Roi
from sculptcut.fixtures import generate_fixture
from sculptcut.i18n import translate
from sculptcut.localization import stage_text
from sculptcut.media import Cancelled
from sculptcut.planning import build_plan, plan_project
from sculptcut.project import create_project, database
from sculptcut.work_episodes import work_catalogue


@pytest.fixture
def analysed(tmp_path):
    source = generate_fixture(tmp_path/'source.mp4', 'local', duration=4, fps=10)
    root = tmp_path/'project'
    project = create_project(root, [source])
    project = project.model_copy(update={'settings': project.settings.model_copy(update={
        'proxy_width': 128, 'proxy_fps': 5, 'target_seconds': 1, 'final_view_seconds': 0})})
    extract_features(root, project)
    return root, project


def unexpected(*args, **kwargs):
    raise AssertionError('Recomputed cached analysis')


def test_complete_legacy_activity_is_reused_and_missing_row_is_repaired(analysed, monkeypatch):
    root, project = analysed
    activity.classify_project(root, project)
    assert activity.activity_ready(root, project, project.sources[0])
    messages = []
    importance.importance_project(root, project, stage=messages.append)
    assert messages == ['Обновляем оценки из сохранённых измерений']
    with monkeypatch.context() as patch:
        patch.setattr(activity, 'classify', unexpected)
        patch.setattr(activity, 'analyze_motion', unexpected)
        activity.classify_project(root, project)
    connection = database(root)
    before = connection.execute('SELECT * FROM activity ORDER BY timestamp').fetchall()
    connection.execute('DELETE FROM activity WHERE timestamp=(SELECT MIN(timestamp) FROM activity)')
    connection.commit()
    connection.close()
    assert not activity.activity_ready(root, project, project.sources[0])
    activity.classify_project(root, project)
    connection = database(root)
    assert connection.execute('SELECT * FROM activity ORDER BY timestamp').fetchall() == before
    connection.close()


def test_importance_reuses_analysis_across_modes_music_duration_and_manual_cuts(
    analysed, monkeypatch,
):
    root, project = analysed
    first = importance.importance_project(root, project)
    features = list(read_features(root, project, project.sources[0]))
    changed = project.model_copy(update={
        'settings': project.settings.model_copy(update={'preset': 'music_shorts',
            'target_seconds': 3, 'music_path': root/'different.wav', 'output_name': 'Other'}),
        'overrides': [Override(source_id=project.sources[0].id, start_us=0,
                              end_us=1_000_000, action='DROP')]})
    messages = []
    monkeypatch.setattr(importance, 'score_segment', unexpected)
    monkeypatch.setattr(importance, 'segment_project', unexpected)
    assert importance.importance_project(root, changed, stage=messages.append) == first
    assert messages == ['Используем сохранённый анализ видео']
    assert translate(messages[0], 'en') == 'Using cached video analysis'
    assert stage_text(messages[0])
    assert list(read_features(root, project, project.sources[0])) == features
    cancelled = Event()
    cancelled.set()
    with pytest.raises(Cancelled):
        importance.importance_project(root, changed, cancelled)


def test_roi_and_activity_policy_invalidate_derived_analysis(analysed, monkeypatch):
    root, project = analysed
    importance.importance_project(root, project)
    calls = []
    original = importance.score_segment
    def score(*args):
        calls.append(args[0].source_id)
        return original(*args)
    monkeypatch.setattr(importance, 'score_segment', score)
    roi = project.model_copy(update={'rois': {project.sources[0].id:
        Roi(x=.1, y=.1, width=.8, height=.8)}})
    extract_features(root, roi)
    importance.importance_project(root, roi)
    assert calls
    calls.clear()
    monkeypatch.setattr(activity, 'SETTINGS', replace(activity.SETTINGS, stable_hash_run_bits=9))
    importance.importance_project(root, roi)
    assert calls


def test_cancelled_classification_cannot_masquerade_as_complete(analysed, monkeypatch):
    root, project = analysed
    cancel = Event()
    original = activity.classify
    def classify(*args):
        cancel.set()
        return original(*args)
    with monkeypatch.context() as patch:
        patch.setattr(activity, 'classify', classify)
        with pytest.raises(Cancelled):
            activity.classify_project(root, project, cancel)
    assert not activity.activity_ready(root, project, project.sources[0])
    activity.classify_project(root, project)
    assert activity.activity_ready(root, project, project.sources[0])


def test_cancelled_importance_is_not_published_as_ready(analysed, monkeypatch):
    root, project = analysed
    cancel = Event()
    original = importance.score_segment
    def cancelled(*args):
        result = original(*args)
        cancel.set()
        return result
    with monkeypatch.context() as patch:
        patch.setattr(importance, 'score_segment', cancelled)
        with pytest.raises(Cancelled):
            importance.importance_project(root, project, cancel)
    calls = []
    def measured(*args):
        calls.append(args[0].source_id)
        return original(*args)
    monkeypatch.setattr(importance, 'score_segment', measured)
    importance.importance_project(root, project)
    assert calls  # The interrupted result was not accepted as a complete cache hit.


def test_verified_catalogue_reuses_saved_scores_and_distinguishes_status(analysed, monkeypatch):
    from sculptcut.development import DevelopmentEvidence
    root, project = analysed
    calls = []
    def score(*args):
        calls.append(args)
        return DevelopmentEvidence(.8)
    messages = []
    first = work_catalogue(root, project, build_plan(project, [], 1), [], score, None,
                           messages.append)
    assert calls and any(m.startswith('Проверяем') for m in messages)
    messages.clear()
    monkeypatch.setattr('sculptcut.work_episodes.process_score', unexpected)
    changed = project.model_copy(update={'settings': project.settings.model_copy(update={
        'preset': 'shorts', 'target_seconds': 3})})
    assert work_catalogue(root, changed, build_plan(changed, [], 3), [], unexpected, None,
                          messages.append) == first
    assert messages == ['Используем сохранённый анализ эпизодов: 1 / 1']
    assert translate(messages[0], 'en') == 'Using cached episode analysis: 1 / 1'
    # Upgrade from older builds: individual window scores are already useful
    # even before the first whole-catalogue result has been saved.
    connection = database(root)
    connection.execute("DELETE FROM cache WHERE stage='verified_catalogue'")
    connection.commit()
    connection.close()
    messages.clear()
    assert work_catalogue(root, changed, build_plan(changed, [], 3), [], unexpected, None,
                          messages.append) == first
    assert any('из кэша' in m and 'новых: 0' in m for m in messages)
    assert not any(m.startswith('Проверяем') for m in messages)


def test_changing_one_roi_only_rescores_that_source(analysed, monkeypatch, tmp_path):
    _, original = analysed
    root = tmp_path/'two-sources'
    second = generate_fixture(tmp_path/'second.mp4', 'static', duration=2, fps=10)
    project = create_project(root, [original.sources[0].path, second])
    project = project.model_copy(update={'settings': original.settings})
    extract_features(root, project)
    importance.importance_project(root, project)
    calls = []
    score = importance.score_segment
    def measured(segment, features):
        calls.append(segment.source_id)
        return score(segment, features)
    monkeypatch.setattr(importance, 'score_segment', measured)
    changed = project.model_copy(update={'rois': {project.sources[0].id:
        Roi(x=.1, y=.1, width=.8, height=.8)}})
    extract_features(root, changed)
    importance.importance_project(root, changed)
    assert set(calls) == {project.sources[0].id}


def test_window_cache_upgrades_legacy_and_invalidates_proxy_and_quality(analysed, monkeypatch):
    import sculptcut.shorts_quality as quality
    from sculptcut.development import DevelopmentEvidence
    from sculptcut.project import cache_put
    from sculptcut.work_episodes import legacy_process_key, process_score
    root, project = analysed
    sid = project.sources[0].id
    cache_put(root, 'work_process', f'{sid}:0:1000000',
              legacy_process_key(project, sid, 0, 1_000_000), {'score': .8})
    assert process_score(root, project, unexpected, sid, 0, 1_000_000) == .8
    calls = []
    def score(*args):
        calls.append(args)
        return DevelopmentEvidence(.6)
    resized = project.model_copy(update={'settings': project.settings.model_copy(
        update={'proxy_width': 256})})
    assert process_score(root, resized, score, sid, 0, 1_000_000) == .6
    assert len(calls) == 1
    assert process_score(root, resized, unexpected, sid, 0, 1_000_000) == .6
    monkeypatch.setattr(quality, 'QUALITY', replace(quality.QUALITY, samples=7))
    assert process_score(root, resized, score, sid, 0, 1_000_000) == .6
    assert len(calls) == 2


def test_legacy_score_older_than_current_proxy_is_not_admitted(analysed):
    from sculptcut.development import DevelopmentEvidence
    from sculptcut.project import cache_put
    from sculptcut.proxy import make_proxies
    from sculptcut.work_episodes import legacy_process_key, process_score
    root, project = analysed
    sid = project.sources[0].id
    cache_put(root, 'work_process', f'{sid}:0:1000000',
              legacy_process_key(project, sid, 0, 1_000_000), {'score': .8})
    resized = project.model_copy(update={'settings': project.settings.model_copy(
        update={'proxy_width': 256})})
    make_proxies(root, resized)
    calls = []
    def score(*args):
        calls.append(args)
        return DevelopmentEvidence(.6)
    assert process_score(root, resized, score, sid, 0, 1_000_000) == .6
    assert len(calls) == 1


def test_filename_only_create_reuses_edit_plan(analysed, monkeypatch):
    root, project = analysed
    first = plan_project(root, project, 1)
    renamed = project.model_copy(update={'settings': project.settings.model_copy(
        update={'output_name': 'New movie'})})
    monkeypatch.setattr(importance, 'importance_project', unexpected)
    messages = []
    assert plan_project(root, renamed, 1, stage=messages.append) == first
    assert messages[-1] == 'Используем сохранённый монтажный план'
