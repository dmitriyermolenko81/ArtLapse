"""User actions preview pending selections and expose only the essential controls."""

from pathlib import Path

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from test_episode_panel import wait_until
from test_episode_roll import roll_movie as roll_movie

from sculptcut.selection_timeline import SelectionTimeline
from sculptcut.source_selection_dialog import SourceSelectionDialog


def test_mouse_selection_preserves_multi_hour_source_timestamps():
    app = QApplication.instance() or QApplication([])
    timeline = SelectionTimeline()
    timeline.resize(1000, 100)
    timeline.duration = 14_400_000_000
    timeline.set_view(10_360_000_000, 40_000_000)
    received = []
    timeline.selected.connect(lambda a, b: received.append((a, b)))
    try:
        timeline.show()
        app.processEvents()
        a, b = 10_381_060_000, 10_386_580_000
        timeline.start, timeline.end = a, b - 2_000_000
        QTest.mousePress(timeline, Qt.MouseButton.LeftButton,
                        pos=QPoint(round(timeline.x_at(timeline.end)), 45))
        QTest.mouseMove(timeline, QPoint(round(timeline.x_at(b)), 45))
        QTest.mouseRelease(timeline, Qt.MouseButton.LeftButton,
                          pos=QPoint(round(timeline.x_at(b)), 45))
        assert received == [(timeline.start, timeline.end)]
        assert abs(received[0][0] - a) < 50_000
        assert abs(received[0][1] - b) < 50_000
    finally:
        timeline.close()


def select_with_mouse(dialog, start, end):
    dialog.timeline.set_view(0, 12_000_000)
    for edge, target in (("end", end), ("start", start)):
        x = round(dialog.timeline.x_at(getattr(dialog.timeline, edge)))
        QTest.mousePress(dialog.timeline, Qt.MouseButton.LeftButton,
                         pos=QPoint(x, 45))
        QTest.mouseMove(dialog.timeline, QPoint(round(dialog.timeline.x_at(target)), 45))
        QTest.mouseRelease(dialog.timeline, Qt.MouseButton.LeftButton,
                           pos=QPoint(round(dialog.timeline.x_at(target)), 45))


def test_preview_plays_exact_unapplied_mouse_selection_not_original_scene(roll_movie):
    app = QApplication.instance() or QApplication([])
    root, _, _, _, video = roll_movie
    owner = QWidget()
    dialog = SourceSelectionDialog(root, video, owner)
    frames = []
    try:
        dialog.show()
        wait_until(app, lambda: not dialog.loading)
        assert not dialog.details.isVisible()
        assert not dialog.start.isVisible() and not dialog.speed.isVisible()
        select_with_mouse(dialog, 4_000_000, 5_500_000)
        assert dialog.keep.isEnabled() and not dialog.drop.isEnabled()
        assert not dialog.session.changed
        dialog.playback.image_ready.connect(lambda image, t: frames.append(t))
        QTest.mouseClick(dialog.play, Qt.MouseButton.LeftButton)
        wait_until(app, lambda: bool(frames))
        assert all(3980 <= t < 5520 for t in frames), frames
        assert 3980 <= dialog.playback.start < 4020
        assert 5480 <= dialog.playback.end < 5520
        dialog.playback.pause()
        assert not dialog.session.changed
        assert dialog.keep.isEnabled()
        dialog.keep.click()
        assert dialog.session.changed and dialog.montage.count() == 4
    finally:
        dialog.shutdown()
        wait_until(app, lambda: dialog.preparer.cleaned)
        owner.close()
        app.processEvents()


def test_neighbour_preview_uses_pending_range_before_apply_without_writing_project(roll_movie):
    app = QApplication.instance() or QApplication([])
    root, _, _, _, video = roll_movie
    owner = QWidget()
    dialog = SourceSelectionDialog(root, video, owner)
    before = (root / "project.json").read_bytes()
    try:
        dialog.show()
        wait_until(app, lambda: not dialog.loading)
        select_with_mouse(dialog, 4_000_000, 5_500_000)
        a, b = round(dialog.start.value() * 1e6), round(dialog.end.value() * 1e6)
        dialog.more.click()
        assert dialog.details.isVisible() and dialog.speed.isVisible()
        dialog.context.click()
        wait_until(app, lambda: dialog.context_ready)
        assert any(
            d.source_start_us == a and d.source_end_us == b and d.output_frame_count
            for d in dialog.preview_plan.decisions
        )
        assert not any(
            d.source_start_us == a and d.output_frame_count for d in dialog.session.plan.decisions
        )
        assert not dialog.session.changed and not dialog.apply.isEnabled()
        assert Path(dialog.playback.player.source().toLocalFile()).name == "context.mp4"
        assert (root / "project.json").read_bytes() == before
    finally:
        dialog.shutdown()
        wait_until(app, lambda: dialog.preparer.cleaned)
        owner.close()
        app.processEvents()


def test_selection_inside_scene_has_one_clear_trim_action_and_undo(roll_movie):
    app = QApplication.instance() or QApplication([])
    root, _, _, _, video = roll_movie
    owner = QWidget()
    dialog = SourceSelectionDialog(root, video, owner)
    try:
        dialog.show()
        wait_until(app, lambda: not dialog.loading)
        assert not dialog.keep.isEnabled() and dialog.drop.isEnabled()
        select_with_mouse(dialog, 500_000, 1_500_000)
        assert dialog.primary_action == "resize" and dialog.keep.isEnabled()
        plan = dialog.pending_selection().plan
        active = [d for d in plan.decisions if d.output_frame_count]
        assert abs(active[0].source_start_us - 500_000) < 30000
        assert abs(active[0].source_end_us - 1_500_000) < 30000
        dialog.keep.click()
        active = [d for d in dialog.session.plan.decisions if d.output_frame_count]
        assert active[0].source_end_us < 1_600_000
        dialog.undo.click()
        assert not dialog.session.changed
    finally:
        dialog.shutdown()
        wait_until(app, lambda: dialog.preparer.cleaned)
        owner.close()
        app.processEvents()

def test_scrub_and_episode_navigation_keep_selection_and_use_original(roll_movie):
    app = QApplication.instance() or QApplication([])
    root, _, _, _, video = roll_movie
    owner = QWidget()
    dialog = SourceSelectionDialog(root, video, owner)
    try:
        dialog.show()
        wait_until(app, lambda: not dialog.loading)
        dialog.next_scene.click()
        assert dialog.montage.currentRow() == 1
        dialog.previous_scene.click()
        assert dialog.montage.currentRow() == 0
        a, b = dialog.timeline.start, dialog.timeline.end
        QTest.mouseClick(dialog.timeline, Qt.MouseButton.LeftButton,
                         pos=QPoint(round(dialog.timeline.x_at(1_000_000)), 15))
        assert (dialog.timeline.start, dialog.timeline.end) == (a, b)
        wait_until(app, lambda: abs(dialog.playback.seek_target - 1000) < 50)
        source = next(s for s in dialog.session.project.sources
                      if s.id == dialog.sources.currentData())
        assert Path(dialog.playback.player.source().toLocalFile()) == source.path.resolve()
        dialog.seek_montage(dialog.montage_seek.blocks[1][1] + 100)
        assert dialog.montage.currentRow() == 1
        assert not dialog.session.changed
    finally:
        dialog.shutdown()
        wait_until(app, lambda: dialog.preparer.cleaned)
        owner.close()
        app.processEvents()


def test_click_preserves_bounds_and_handle_previews_before_release(roll_movie):
    app = QApplication.instance() or QApplication([])
    root, _, _, _, video = roll_movie
    owner = QWidget()
    dialog = SourceSelectionDialog(root, video, owner, compact=True)
    frames = []
    try:
        dialog.show()
        wait_until(app, lambda: not dialog.loading)
        dialog.timeline.show()
        app.processEvents()
        wait_until(app, lambda: not dialog.frame_image.isNull() and dialog.preparer.worker is None)
        dialog.timeline.set_view(0, 12_000_000)
        dialog.playback.image_ready.connect(lambda image, t: frames.append(t))
        dialog.scrub_frames.ready.connect(
            lambda key, image: frames.append(dialog.timeline.playhead_us // 1000))
        a, b = dialog.timeline.start, dialog.timeline.end
        QTest.mouseClick(dialog.timeline, Qt.MouseButton.LeftButton,
                         pos=QPoint(round(dialog.timeline.x_at(5_000_000)), 45))
        assert (dialog.timeline.start, dialog.timeline.end) == (a, b)
        wait_until(app, lambda: any(4800 <= t <= 5200 for t in frames))
        frames.clear()
        QTest.mousePress(dialog.timeline, Qt.MouseButton.LeftButton,
                         pos=QPoint(round(dialog.timeline.x_at(b)), 45))
        QTest.mouseMove(dialog.timeline, QPoint(round(dialog.timeline.x_at(6_000_000)), 45))
        wait_until(app, lambda: any(5800 <= t <= 6200 for t in frames))
        assert dialog.timeline.drag == "end"
        QTest.mouseRelease(dialog.timeline, Qt.MouseButton.LeftButton,
                           pos=QPoint(round(dialog.timeline.x_at(6_000_000)), 45))
        wait_until(app, lambda: not dialog.scrub_timer.isActive())
        assert abs(dialog.end.value() - 6) < .05
        assert 5_800_000 <= dialog.timeline.playhead_us <= 6_200_000
        assert not dialog.session.changed
    finally:
        dialog.shutdown()
        wait_until(app, lambda: dialog.preparer.cleaned)
        owner.close()
        app.processEvents()

def test_compact_editor_reuses_main_preview_and_switches_whole_trim(roll_movie):
    from sculptcut.ui import MainWindow

    app = QApplication.instance() or QApplication([])
    root, _, _, _, video = roll_movie
    window = MainWindow()
    try:
        window.folder.setText(str(root))
        window.show()
        window.open_video(video, "test")
        window.player.pause()
        viewer = window.viewer_scroll.widget()
        window.cut_button.click()

        dialog = window.selection_dialog
        wait_until(app, lambda: not dialog.loading and not dialog.frame_image.isNull())
        assert window.viewer_scroll.widget() is viewer
        assert window.video_frame_image == dialog.frame_image
        assert not dialog.montage.isVisible() and not dialog.picture.isVisible()
        window.play_button.click()
        assert dialog.sequence.desired
        window.play_button.click()
        assert not dialog.sequence.desired
        assert not window.trim_mode.isVisible()
        assert not dialog.timeline.isVisible()
        assert dialog.montage_seek.isVisible()
        window.cut_button.click()
        assert window.selection_dialog is dialog and not dialog.isVisible()
        assert window.viewer_scroll.widget() is viewer
    finally:
        window._allow_close = True
        window.close()
        app.processEvents()

def test_single_track_edges_apply_automatically_and_movie_trim_saves(roll_movie):
    from sculptcut.ui import MainWindow
    from sculptcut.variants import read_variant

    app = QApplication.instance() or QApplication([])
    root, _, _, _, video = roll_movie
    window = MainWindow()
    errors = []
    window.show_error = errors.append
    try:
        window.folder.setText(str(root))
        window.show()
        window.open_video(video, "test")
        window.player.pause()
        window.cut_button.click()
        dialog = window.selection_dialog
        wait_until(app, lambda: not dialog.loading)
        track = dialog.montage_seek
        assert not window.trim_mode.isVisible()
        assert not dialog.timeline.isVisible() and not dialog.keep.isVisible()
        row, a, b = track.blocks[0]
        old = dialog.session.plan
        target = (a+b)//2
        QTest.mousePress(track, Qt.MouseButton.LeftButton,
                         pos=QPoint(round(track.x_at(b)), 22))
        QTest.mouseMove(track, QPoint(round(track.x_at(target)), 22))
        wait_until(app, lambda: not dialog.scrub_timer.isActive())
        assert not dialog.session.changed
        QTest.mouseRelease(track, Qt.MouseButton.LeftButton,
                           pos=QPoint(round(track.x_at(target)), 22))
        assert dialog.session.changed
        assert dialog.session.plan.actual_frame_count == old.actual_frame_count
        dialog.undo.click()
        assert dialog.session.plan == old
        total = track.maximum()
        QTest.mousePress(track, Qt.MouseButton.LeftButton,
                         pos=QPoint(round(track.x_at(total)), 8))
        QTest.mouseMove(track, QPoint(round(track.x_at(total-800)), 8))
        QTest.mouseRelease(track, Qt.MouseButton.LeftButton,
                           pos=QPoint(round(track.x_at(total-800)), 8))
        assert dialog.session.changed
        plan = dialog.session.plan
        assert plan.actual_frame_count == old.actual_frame_count
        # Also shorten the presentation at the start, without touching the body again.
        total = track.maximum()
        QTest.mousePress(track, Qt.MouseButton.LeftButton,
                         pos=QPoint(round(track.x_at(0)), 8))
        QTest.mouseMove(track, QPoint(round(track.x_at(500)), 8))
        QTest.mouseRelease(track, Qt.MouseButton.LeftButton,
                           pos=QPoint(round(track.x_at(500)), 8))
        plan = dialog.session.plan
        dialog.apply.click()
        wait_until(app, lambda: window.job is None, 25)
        assert not errors, errors
        result = next((root / "previews").glob("*selection*.mp4"))
        assert read_variant(result)["plan"]["actual_frame_count"] == plan.actual_frame_count
        from sculptcut.rendering import verify_output

        verify_output(result, round(dialog.session.project.settings.target_seconds*plan.output_fps),
                      plan.output_fps, expected_audio=True)
    finally:
        window._allow_close = True
        window.close()
        app.processEvents()

def test_scene_body_drag_click_jitter_and_escape_never_edit_montage(roll_movie):
    from sculptcut.ui import MainWindow

    app = QApplication.instance() or QApplication([])
    root, _, _, _, video = roll_movie
    window = MainWindow()
    try:
        window.folder.setText(str(root))
        window.show()
        window.open_video(video, "test")
        window.player.pause()
        window.cut_button.click()
        dialog = window.selection_dialog
        wait_until(app, lambda: not dialog.loading)
        track = dialog.montage_seek
        original = dialog.session.plan
        row, a, b = track.blocks[0]
        x = round(track.x_at(b))
        # The line inside a clip is for navigation, not a resize target.
        QTest.mousePress(track, Qt.MouseButton.LeftButton, pos=QPoint(x, 40))
        QTest.mouseMove(track, QPoint(x-35, 40))
        QTest.mouseRelease(track, Qt.MouseButton.LeftButton, pos=QPoint(x-35, 40))
        assert dialog.session.plan == original and not dialog.session.undo_stack
        # A handle click or natural hand jitter must not quantize or split a scene.
        for y in (22,):
            QTest.mousePress(track, Qt.MouseButton.LeftButton, pos=QPoint(x, y))
            QTest.mouseMove(track, QPoint(x+2, y))
            QTest.mouseRelease(track, Qt.MouseButton.LeftButton, pos=QPoint(x+2, y))
            assert dialog.session.plan == original and not dialog.session.undo_stack
        # Escape cancels a deliberate drag before the left button is released.
        QTest.mousePress(track, Qt.MouseButton.LeftButton, pos=QPoint(x, 22))
        QTest.mouseMove(track, QPoint(x-30, 22))
        QTest.keyClick(track, Qt.Key.Key_Escape)
        QTest.mouseRelease(track, Qt.MouseButton.LeftButton, pos=QPoint(x-30, 22))
        assert dialog.session.plan == original and not dialog.session.undo_stack
        x = round(track.x_at(track.maximum()))
        QTest.mouseClick(track, Qt.MouseButton.LeftButton, pos=QPoint(x, 8))
        assert dialog.session.plan == original and not dialog.session.undo_stack
        # Normal motion after a release cannot continue editing a prior handle.
        QTest.mouseMove(track, QPoint(x-100, 8))
        assert dialog.session.plan == original and not dialog.session.undo_stack
    finally:
        window._allow_close = True
        window.close()
        app.processEvents()

def test_short_scene_has_no_overlapping_resize_targets():
    from sculptcut.simple_timeline import SimpleTimeline

    app = QApplication.instance() or QApplication([])
    track = SimpleTimeline()
    track.resize(600, 86)
    track.setRange(0, 10000)
    track.blocks = [(0, 1000, 1010)]
    track.selected_row = 0
    edits = []
    track.edge_commit.connect(lambda *args: edits.append(args))
    try:
        track.show()
        app.processEvents()
        x = round(track.x_at(1000))
        QTest.mousePress(track, Qt.MouseButton.LeftButton, pos=QPoint(x, 22))
        QTest.mouseMove(track, QPoint(x+60, 22))
        QTest.mouseRelease(track, Qt.MouseButton.LeftButton, pos=QPoint(x+60, 22))
        assert not edits
    finally:
        track.close()
