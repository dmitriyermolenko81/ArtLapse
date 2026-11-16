"""Uncertain drawing comparisons must not silently remove the whole session."""

from pathlib import Path

import pytest

from sculptcut.domain import ProjectSettings
from sculptcut.edit_stats import statistics_text
from sculptcut.fixtures import generate_fixture
from sculptcut.idle import IdleRange
from sculptcut.photoshop_story import episodic_photoshop_plan
from sculptcut.planning import build_plan, validate_plan
from sculptcut.project import create_project
from sculptcut.short_catalogue import complete_short_catalogue
from sculptcut.work_episodes import WorkEpisode


@pytest.mark.parametrize('confirmed', [False, True])
def test_unknown_drawing_retains_budget_without_restoring_drops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, confirmed: bool,
) -> None:
    source = generate_fixture(tmp_path / 'source.mp4', 'local', duration=96, fps=1)
    root = tmp_path / 'project'
    project = create_project(root, [source])
    project = project.model_copy(update={'settings': ProjectSettings(
        preset='photoshop_shorts', target_seconds=60)})
    sid = project.sources[0].id
    pool = [WorkEpisode(source_id=sid, start_us=i*6_000_000, end_us=(i+1)*6_000_000,
        kind='UNKNOWN', score=.5, process_checked=True,
        process_verified=confirmed and i == 0,
        process_note=('confirmed_change' if confirmed and i == 0 else
                      'no_persistent_change' if i == 1 else 'incomparable_or_occluded'))
        for i in range(16)]
    monkeypatch.setattr('sculptcut.short_catalogue.make_proxies', lambda *args: None)
    monkeypatch.setattr('sculptcut.short_catalogue.work_catalogue', lambda *args, **kw: pool)
    monkeypatch.setattr('sculptcut.screen_cleanup.project_screens', lambda *args: [])
    base = build_plan(project, [IdleRange(source_id=sid, start_us=12_000_000,
        end_us=18_000_000, reason='user:DROP')], 60)
    plan = complete_short_catalogue(root, project, base, [])
    result = episodic_photoshop_plan(project, plan)
    validate_plan(project, result)
    assert result.actual_frame_count == 1800
    assert result.settings['short_candidates_verified'] == int(confirmed)
    assert result.settings['short_candidates_retained'] == 15
    assert 'После анализа: 15' in statistics_text(result)
    assert not result.settings.get('short_catalogue_fallback')
    assert all(not d.output_frame_count or d.source_end_us <= 6_000_000
               or d.source_start_us >= 18_000_000 for d in result.decisions)


@pytest.mark.parametrize('overlays',[False,True])
def test_verified_intermediate_states_are_not_displaced_by_unknown_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overlays: bool,
) -> None:
    source = generate_fixture(tmp_path/'source.mp4','local',duration=240,fps=1)
    root = tmp_path/'project'
    project = create_project(root,[source]).model_copy(update={'settings':ProjectSettings(
        preset='photoshop_shorts',target_seconds=12)})
    sid = project.sources[0].id
    pool = [WorkEpisode(source_id=sid,start_us=i*6_000_000,end_us=(i+1)*6_000_000,
        kind='UNKNOWN',score=.5,process_checked=True,process_verified=i%3==0,
        work_start_us=i*6_000_000 if i%3==0 else None,
        work_end_us=i*6_000_000+3_360_000 if i%3==0 else None,
        process_note='confirmed_change' if i%3==0 else 'incomparable_or_occluded')
        for i in range(40)]
    monkeypatch.setattr('sculptcut.short_catalogue.make_proxies',lambda *args:None)
    monkeypatch.setattr('sculptcut.short_catalogue.work_catalogue',lambda *args,**kw:pool)
    screens = [IdleRange(source_id=sid,start_us=e.start_us,end_us=e.end_us,
                        reason='notification_overlay') for e in pool[:-1]
               if e.process_verified] if overlays else []
    monkeypatch.setattr('sculptcut.screen_cleanup.project_screens',lambda *args:screens)
    base = complete_short_catalogue(root,project,build_plan(project,[],12),[])
    plan = episodic_photoshop_plan(project,base)
    assert plan.actual_frame_count == 360
    if overlays:
        assert plan.settings['short_unknown_policy'] == 'retain-drawing-360'
        unknown_ids = {e.id for e in pool if not e.process_verified}
        assert any(d.output_frame_count and d.episode_id in unknown_ids for d in plan.decisions)
    else:
        assert plan.settings['short_unknown_policy'] == 'prefer-verified-363'
        assert {d.episode_id for d in plan.decisions if d.output_frame_count} >= {
            e.id for e in pool if e.process_verified}
