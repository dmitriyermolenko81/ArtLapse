"""Legacy speed baselines survive a missing or incomplete parent snapshot."""
import json

import pytest
from test_timeline import make_source

from sculptcut.combined_edits import _original_speed_baseline
from sculptcut.domain import Override, Project
from sculptcut.export_storage import metadata_path


@pytest.mark.parametrize('parent', [None, {}, {'schema_version': 1}, {'broken': True}])
def test_missing_or_incomplete_parent_keeps_last_usable_snapshot(tmp_path, parent):
    project = Project(sources=[make_source(0, 0, 10_000_000)], overrides=[Override(
        source_id='0', start_us=0, end_us=1_000_000, action='SET_SPEED', speed=2)])
    video = tmp_path/'renders/current.mp4'
    previous = video.with_name('previous.mp4')
    path = metadata_path(previous, '.variant.json')
    if parent is not None:
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(parent), encoding='utf-8')
    state = {'project': project.model_dump(mode='json'), 'episode_base_plan': {},
             'parent': previous.name}
    state['episode_base_plan'] = {'legacy': True}
    before = json.dumps(state)
    result = json.loads(_original_speed_baseline(video, state))
    assert result == [project.overrides[0].model_dump(mode='json')]
    assert json.dumps(state) == before


def test_valid_parent_is_followed_before_a_missing_grandparent(tmp_path):
    project = Project(sources=[make_source(0, 0, 10_000_000)])
    video = tmp_path/'renders/current.mp4'
    parent = video.with_name('parent.mp4')
    path = metadata_path(parent, '.variant.json')
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'schema_version': 1, 'project': project.model_dump(mode='json'),
        'episode_base_plan': {'legacy': True}, 'parent': 'removed.mp4'}), encoding='utf-8')
    current = project.model_copy(update={'overrides': [Override(source_id='0', start_us=0,
        end_us=1_000_000, action='SET_SPEED', speed=3)]})
    result = _original_speed_baseline(video, {'project': current.model_dump(mode='json'),
        'episode_base_plan': {'legacy': True}, 'parent': parent.name})
    assert json.loads(result) == []
