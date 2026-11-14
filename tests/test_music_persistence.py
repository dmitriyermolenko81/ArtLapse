"""Damaged persisted lanes fail safely, preserving valid tails and silence."""
import json

import pytest
from test_music_edit import music_movie as music_movie

from sculptcut.music_edit import (
    MusicClip,
    draft_path,
    load_draft,
    parse_music_clips,
    save_draft,
)


@pytest.mark.parametrize('payload', [None, [], {}, {'version': 1}, 'bad'])
def test_incomplete_draft_reports_value_error(tmp_path, payload):
    video = tmp_path/'video.mp4'
    video.write_bytes(b'fixture')
    path = draft_path(tmp_path, video)
    path.parent.mkdir()
    path.write_text(json.dumps(payload), encoding='utf-8')
    before = path.read_bytes()
    with pytest.raises(ValueError, match='черновик'):
        load_draft(tmp_path, video)
    assert path.read_bytes() == before


@pytest.mark.parametrize('payload', [None, {}, 'bad', [None]])
def test_invalid_lane_is_rejected(payload):
    with pytest.raises(ValueError):
        parse_music_clips(payload)


def test_overlapping_draft_is_rejected_but_tails_and_silence_survive(tmp_path):
    video = tmp_path/'video.mp4'
    video.write_bytes(b'fixture')
    clip = MusicClip(path=video, start_us=100, end_us=1000, at_us=0)
    other = clip.model_copy(update={'at_us': 500})
    save_draft(tmp_path, video, [clip, other])
    with pytest.raises(ValueError, match='перекрываться'):
        load_draft(tmp_path, video)
    tail = other.model_copy(update={'at_us': 2000})
    save_draft(tmp_path, video, [clip, tail])
    assert load_draft(tmp_path, video) == [clip, tail]
    save_draft(tmp_path, video, [])
    assert load_draft(tmp_path, video) == []


@pytest.mark.parametrize('settings', [{'preset': []}, {'preset': {}}, 'bad', [1]])
def test_malformed_saved_preset_reports_value_error(tmp_path, settings):
    from sculptcut.export_storage import metadata_path
    from sculptcut.music_edit import music_edit_locked

    video = tmp_path/'video.mp4'
    path = metadata_path(video, '.variant.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'schema_version': 1, 'project': {'settings': settings}}),
                    encoding='utf-8')
    with pytest.raises(ValueError):
        music_edit_locked(video)


@pytest.mark.parametrize('payload', [None, [], 'bad', 12])
def test_video_draft_wrong_root_fails_before_changing_session(tmp_path, payload):
    from sculptcut.selection_draft import draft_path, restore_draft

    video = tmp_path/'video.mp4'
    video.write_bytes(b'fixture')
    path = draft_path(tmp_path, video)
    path.parent.mkdir()
    path.write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(ValueError, match='черновик'):
        restore_draft(tmp_path, video, None)


@pytest.mark.parametrize('kind', ['video', 'music'])
def test_bad_draft_does_not_leave_real_editor_loading(music_movie, kind):
    from PySide6.QtWidgets import QApplication
    from test_cursor_playback import finish, open_editor

    from sculptcut.selection_draft import draft_path as video_draft_path

    root, video, _ = music_movie
    path = (video_draft_path if kind == 'video' else draft_path)(root, video)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[]', encoding='utf-8')
    app = QApplication.instance() or QApplication([])
    window, editor = open_editor(root, video, app)
    try:
        assert not editor.loading
        assert editor.message.isHidden()
        assert editor.layout_box.indexOf(editor.message) == -1
        assert 'черновик' in window.status.text().lower()
        assert 'черновик' in editor.message.text().lower()
        assert path.read_text(encoding='utf-8') == '[]'
        assert (editor.music_track is not None) == (kind == 'video')
    finally:
        finish(window, editor, app)
