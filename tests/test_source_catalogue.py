"""One source partition survives mode, duration, music and manual exclusion changes."""
from pathlib import Path

from test_source_selection import session

from sculptcut.domain import Override, Project
from sculptcut.planning import build_plan, validate_plan
from sculptcut.shorts_layout import ACTIVE_PRESETS
from sculptcut.source_catalogue import catalogue, split_plan
from sculptcut.work_episodes import work_catalogue


def test_source_catalogue_has_identical_boundaries_for_all_modes(tmp_path):
    edit = session()
    expected = catalogue(tmp_path, edit.project)
    saved = (tmp_path/'source-episodes.json').read_bytes()
    for i, preset in enumerate(ACTIVE_PRESETS):
        project = edit.project.model_copy(update={'settings': edit.project.settings.model_copy(
            update={'preset': preset, 'target_seconds': i+1, 'music_path': Path('different.wav')}),
            'overrides': [Override(source_id='0', start_us=1_000_000, end_us=3_000_000,
                                  action='DROP')]})
        assert catalogue(tmp_path, project) == expected
        assert (tmp_path/'source-episodes.json').read_bytes() == saved
        pool = work_catalogue(tmp_path, project, edit.plan, [], None, None)
        assert [(e.source_id, e.start_us, e.end_us) for e in pool] == expected


def test_q_is_saved_in_project_and_splits_every_mode_without_losing_frames(tmp_path):
    edit = session()
    edit.split_at(400)
    shared = edit.project.source_splits
    assert shared == {'0': [1_200_000]}
    restored = Project.model_validate_json(edit.project.model_dump_json())
    assert restored.source_splits == shared
    for preset in ACTIVE_PRESETS:
        project = restored.model_copy(update={'settings': restored.settings.model_copy(
            update={'preset': preset})})
        plan = build_plan(project, [], 36)
        divided = split_plan(project, plan)
        validate_plan(project, divided)
        assert divided.actual_frame_count == plan.actual_frame_count
        assert any(d.source_end_us == 1_200_000 for d in divided.decisions)
        assert catalogue(tmp_path, project)[0] == ('0', 0, 1_200_000)
    edit.undo()
    assert not edit.project.source_splits
    edit.undo(redo=True)
    assert edit.project.source_splits == shared
