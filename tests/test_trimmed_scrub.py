"""Baked intro cuts keep using saved pixels with the correct shortened-video clock."""
import json
from threading import Event

import pytest
from PySide6.QtWidgets import QApplication
from test_cursor_playback import finish, open_editor
from test_episode_panel import wait_until
from test_episode_roll import roll_movie as roll_movie

from sculptcut.export_storage import metadata_path
from sculptcut.intro import body_start_frame
from sculptcut.output_cuts import cut_output
from sculptcut.variants import read_variant


@pytest.mark.parametrize('bookends', [False, True])
def test_baked_intro_removal_uses_fast_saved_video_instead_of_hours_of_source(roll_movie, bookends):
    app = QApplication.instance() or QApplication([])
    root, project, plan, _, video = roll_movie
    head_ms = round(body_start_frame(project.settings, plan.output_fps,
                                    plan.actual_frame_count)*1000/plan.output_fps)
    trimmed = cut_output(video, [(0, head_ms*1000)], Event(), lambda _: None)
    if bookends:
        state = read_variant(trimmed)
        state['cut_history_us'] = []
        state['cuts_us'] = []
        state['bookend_cuts_us'] = [[0, head_ms*1000]]*2
        metadata_path(trimmed, '.variant.json').write_text(json.dumps(state), encoding='utf8')
    window, dialog = open_editor(root, trimmed, app)
    seen = []
    dialog.scrub_frames.ready.connect(lambda key, image: seen.append(key))
    try:
        assert not dialog.saved_segments_compatible  # Playback guard is intentionally stricter.
        assert dialog.saved_settings_compatible
        for row in range(len(dialog.montage_seek.blocks)):
            cursor = dialog.montage_seek.blocks[row][1]+100
            seen.clear()
            dialog.seek_montage(cursor)
            wait_until(app, lambda: bool(seen))
            assert dialog.saved_scrub
            assert seen[-1][0] == trimmed.resolve()
            assert abs(seen[-1][1]//1000-(cursor-head_ms)) <= 1
        old_next = dialog.montage_seek.blocks[1][1]
        old_count = round((dialog.montage_seek.blocks[1][2]-old_next)*plan.output_fps/1000)
        row, a, b = dialog.montage_seek.blocks[0]
        dialog.commit_edge(row, 'end', (a+b)//2)
        cursor = dialog.montage_seek.blocks[1][1]+100
        seen.clear()
        dialog.seek_montage(cursor)
        wait_until(app, lambda: bool(seen))
        assert seen[-1][0] == trimmed.resolve()
        # Duration is now refitted; 100ms in the new scene addresses fewer
        # frames in its faster saved version. Compare the exact old-file frame.
        _, start, end = dialog.montage_seek.blocks[1]
        new_count = round((end-start)*plan.output_fps/1000)
        expected = (round((old_next-head_ms)*plan.output_fps/1000)
                    + 100*plan.output_fps*old_count//(1000*new_count))
        assert abs(seen[-1][1]//1000-round(expected*1000/plan.output_fps)) <= 1
    finally:
        finish(window, dialog, app)
