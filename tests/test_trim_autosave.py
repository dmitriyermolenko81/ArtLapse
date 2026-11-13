"""Pending edits survive restarts; saved video playback avoids re-encoding and frame drops."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QWidget
from test_cursor_playback import finish, open_editor
from test_episode_panel import wait_until
from test_episode_roll import roll_movie as roll_movie

from sculptcut.combined_edits import prepare_combined_edit
from sculptcut.episode_playback import EpisodePlayback
from sculptcut.selection_draft import draft_path, restore_draft
from sculptcut.source_selection import SourceSelection


def test_autosave_and_top_save_restore_trim_cursor_and_undo_after_restart(roll_movie):
    app = QApplication.instance() or QApplication([])
    root, _, _, _, video = roll_movie
    window, dialog = open_editor(root, video, app)
    before = (root / "project.json").read_bytes()
    row, a, b = dialog.montage_seek.blocks[0]
    dialog.commit_edge(row, "end", (a+b)//2)
    plan = dialog.session.plan
    cursor = dialog.montage_seek.blocks[1][1]+100
    dialog.seek_montage(cursor)
    wait_until(app, lambda: not dialog.scrub_timer.isActive())
    window.autosave_draft()
    path = draft_path(root, video)
    assert path.is_file()
    window.cut_button.click()
    window.save_button.click()
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["plan"]["actual_frame_count"] == plan.actual_frame_count
    finish(window, dialog, app)
    window, restored = open_editor(root, video, app)
    try:
        assert restored.session.plan == plan
        assert restored.session.changed
        assert restored.session.restored_cursor == stored["cursor"]
        assert restored.session.undo_stack
        restored.undo.click()
        assert not restored.session.changed
        assert (root / "project.json").read_bytes() == before
        # Refuse to silently attach a draft to a replaced export.
        data = json.loads(path.read_text(encoding="utf-8"))
        data["size"] += 1
        path.write_text(json.dumps(data), encoding="utf-8")
        fresh = SourceSelection(prepare_combined_edit(root, video, exact_variant=True))
        with pytest.raises(ValueError, match="другой версии"):
            restore_draft(root, video, fresh)
    finally:
        finish(window, restored, app)


def test_unedited_cursor_uses_saved_video_without_preparation(roll_movie):
    app = QApplication.instance() or QApplication([])
    root, _, _, _, video = roll_movie
    window, dialog = open_editor(root, video, app)
    try:
        assert dialog.saved_video_compatible
        dialog.seek_montage(2200)
        window.play_button.click()
        wait_until(app, lambda: dialog.playback.is_advancing(), 10)
        assert dialog.sequence.direct
        assert dialog.sequence.preparer.worker is None
        assert Path(dialog.playback.player.source().toLocalFile()) == video.resolve()
        assert dialog.sequence.initial_cursor == 2200
        assert dialog.sequence.cursor >= 2200
    finally:
        finish(window, dialog, app)


def test_cursor_autosave_does_not_rewrite_the_full_montage_and_restores_position(roll_movie):
    app = QApplication.instance() or QApplication([])
    root, _, _, _, video = roll_movie
    window, dialog = open_editor(root, video, app)
    try:
        window.autosave_draft()
        path = draft_path(root, video)
        before = path.read_bytes()
        stamp = path.stat().st_mtime_ns
        cursor = dialog.montage_seek.blocks[1][1]+100
        dialog.seek_montage(cursor)
        window.autosave_draft()
        assert path.read_bytes() == before and path.stat().st_mtime_ns == stamp
        assert path.with_suffix('.position.json').is_file()
        fresh = SourceSelection(prepare_combined_edit(root, video, exact_variant=True))
        restore_draft(root, video, fresh)
        assert fresh.restored_cursor == cursor
        row, a, b = dialog.montage_seek.blocks[0]
        dialog.commit_edge(row, 'end', (a+b)//2)
        window.autosave_draft()
        assert path.read_bytes() != before
        newer = SourceSelection(prepare_combined_edit(root, video, exact_variant=True))
        restore_draft(root, video, newer)
        assert newer.plan == dialog.session.plan  # Old position sidecar cannot replace new edits.
    finally:
        finish(window, dialog, app)


def test_30fps_jitter_does_not_drop_half_of_displayed_frames(monkeypatch):
    import sculptcut.episode_playback as module

    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    playback = EpisodePlayback(owner)
    times = iter(1+i/30+(0.002 if i%2 else 0) for i in range(30))
    monkeypatch.setattr(module, "monotonic", lambda: next(times))
    monkeypatch.setattr(playback.player, "metaData", lambda: SimpleNamespace(value=lambda key: 30))
    frames = []
    playback.image_ready.connect(lambda image, stamp: frames.append(stamp))
    image = QImage(16, 16, QImage.Format.Format_ARGB32)
    frame = SimpleNamespace(isValid=lambda: True, startTime=lambda: 0, toImage=lambda: image)
    try:
        for _ in range(30):
            playback.frame_ready(frame)
        assert len(frames) == 30
    finally:
        playback.shutdown()
        owner.close()
        app.processEvents()
