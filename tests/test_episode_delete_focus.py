"""Deletion restores the nearest surviving episode, independently of stale playback."""

from pathlib import Path

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu, QWidget
from test_episode_panel import episode_project as episode_project

from sculptcut.domain import Project
from sculptcut.episode_panel import EpisodePanel
from sculptcut.planning import RenderPlan


@pytest.mark.parametrize("hide_dropped", [False, True])
@pytest.mark.parametrize("playing", [False, True])
@pytest.mark.parametrize("deleted, expected", [(0, 1_600_000), (3, 3_200_000), (4, 4_800_000)])
def test_delete_focus_uses_source_neighbour_not_old_playback_or_row(
    episode_project: tuple[Path, Project, RenderPlan], hide_dropped: bool,
    deleted: int, expected: int, playing: bool,
) -> None:
    root, _, plan = episode_project
    first = plan.decisions[0]
    decisions = [first.model_copy(update={
        "source_start_us": index * 1_600_000, "source_end_us": (index + 1) * 1_600_000,
        "output_frame_count": 24,
    }) for index in range(5)] + plan.decisions[3:]
    plan = plan.model_copy(update={"decisions": decisions})
    (root / "render_plan.json").write_text(plan.model_dump_json(), encoding="utf-8")
    app = QApplication.instance() or QApplication([])
    parent = QWidget()
    panel = EpisodePanel(root, parent)
    panel.show()
    try:
        panel.show_dropped.setChecked(not hide_dropped)
        panel.editor.setCurrentRow(0)
        panel.editor.preview_episode(panel.editor.item(0))
        assert panel.playback.start == 0
        if playing:
            panel.play_button.click()
            assert panel.playback.want_play and panel.playback.timer.isActive()
        # A context-menu edit need not have previewed the target first.
        panel.editor.setCurrentRow(deleted)
        panel.editor.choose(panel.editor.item(deleted))
        menu = panel.editor.findChildren(QMenu)[-1]
        menu.actions()[1].trigger()
        menu.close()
        QTest.qWait(150)  # Include the queued restore, multimedia and card callbacks.

        def assert_focus(start: int) -> None:
            decision = panel.timeline.selected_decision()
            assert decision is not None and decision.action != "DROP"
            assert decision.source_start_us == start
            assert panel.editor.currentRow() == panel.timeline.selected
            assert panel.playback.start == start // 1000
            assert not panel.playback.want_play and not panel.playback.timer.isActive()

        assert_focus(expected)
        assert panel.grab().save(str(root.parent / "delete-focus.png"))
        if deleted == 3:
            # Adjacent deleted cards merge; row offsets must not move the anchor.
            panel.editor.apply_choice(panel.editor.currentItem(), "drop")
            QTest.qWait(150)
            assert_focus(1_600_000)
    finally:
        panel.reject()
        parent.close()
        app.processEvents()
