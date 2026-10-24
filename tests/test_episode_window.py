"""Compact episode workspace and project-scoped source-time selection."""

from pathlib import Path

import pytest
from PySide6.QtCore import QSettings, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from test_episode_panel import episode_project as episode_project
from test_episode_panel import wait_until

from sculptcut.domain import Project
from sculptcut.episode_panel import EpisodePanel
from sculptcut.i18n import set_language
from sculptcut.planning import RenderPlan
from sculptcut.ui import MainWindow


def test_real_main_window_first_focus_and_reopen_keeps_source_and_episode(
    episode_project: tuple[Path, Project, RenderPlan],
) -> None:
    app = QApplication.instance() or QApplication([])
    root, _, _ = episode_project
    before = {name: (root / name).read_bytes() for name in ("project.json", "render_plan.json")}
    owner = MainWindow()
    owner.open_project_folder(root)
    errors = []

    def inspect(reopened: bool) -> None:
        panel = owner.episode_panel
        try:
            assert panel is not None and panel.isVisible()
            expected = 5 if reopened else 0
            assert panel.editor.currentRow() == panel.timeline.selected == expected
            assert panel.play_button.isEnabled() and not panel.playback.want_play
            wait_until(app, lambda: not panel.picture_image.isNull())
            if not reopened:
                panel.sources.setCurrentIndex(1)
                panel.editor.setCurrentRow(5)
                panel.editor.preview_episode(panel.editor.item(5))
            assert panel.sources.currentIndex() == 1
        except Exception as exc:
            errors.append(repr(exc))
        finally:
            if panel is not None:
                panel.reject()

    try:
        for reopened in (False, True):
            QTimer.singleShot(0, lambda reopened=reopened: inspect(reopened))
            owner.show_episodes()
        assert not errors, errors
        assert owner.edit_history.position == 0
        assert all((root / name).read_bytes() == value for name, value in before.items())
    finally:
        owner.close()
        app.processEvents()


def test_focus_survives_new_settings_instance_and_changed_card_indices(
    episode_project: tuple[Path, Project, RenderPlan], tmp_path: Path,
) -> None:
    app = QApplication.instance() or QApplication([])
    root, _, plan = episode_project
    ini = str(tmp_path / "view.ini")
    parent = QWidget()
    panel = EpisodePanel(root, parent,
                         view_settings=QSettings(ini, QSettings.Format.IniFormat))
    panel.sources.setCurrentIndex(1)
    panel.editor.setCurrentRow(5)
    panel.editor.preview_episode(panel.editor.item(5))
    panel.reject()
    first = plan.decisions[0]
    midpoint = (first.source_start_us + first.source_end_us) // 2
    split = [first.model_copy(update={"source_end_us": midpoint,
                                     "output_frame_count": first.output_frame_count // 2}),
             first.model_copy(update={"source_start_us": midpoint,
                                      "output_frame_count": first.output_frame_count // 2})]
    updated = plan.model_copy(update={"decisions": [*split, *plan.decisions[1:]]})
    (root / "render_plan.json").write_text(updated.model_dump_json(), encoding="utf-8")
    reopened = EpisodePanel(root, parent,
                            view_settings=QSettings(ini, QSettings.Format.IniFormat))
    try:
        assert reopened.editor.currentRow() == reopened.timeline.selected == 6
        assert reopened.sources.currentIndex() == 1
        assert reopened.timeline.selected_decision().source_start_us == 5_000_000
        assert not reopened.playback.want_play
        # A version's edit context can omit a source while the base plan still
        # covers every source, as required by the saved-plan validator.
        remaining = [d for d in updated.decisions if d.source_id == plan.decisions[0].source_id]
        reopened.editor.context_plan = updated.model_copy(update={
            "decisions": remaining,
            "actual_frame_count": sum(d.output_frame_count for d in remaining),
        })
        reopened.editor.load(root)
        reopened.restore_selection()
        assert reopened.editor.currentRow() == reopened.timeline.selected == 0
        reopened.editor.context_plan = None
        reopened.editor.load(root)
        reopened.restore_selection()
        assert reopened.editor.currentRow() == reopened.timeline.selected == 6
    finally:
        reopened.reject()
        other_root = tmp_path / "other-project"
        other_root.mkdir()
        for name in ("project.json", "render_plan.json"):
            (other_root / name).write_bytes((root / name).read_bytes())
        other = EpisodePanel(other_root, parent,
                             view_settings=QSettings(ini, QSettings.Format.IniFormat))
        try:
            assert other.editor.currentRow() == other.timeline.selected == 0
        finally:
            other.reject()
        parent.close()
        app.processEvents()


@pytest.mark.parametrize("saved", ["invalid", "[]", '{"version":1,"source_id":"gone",'
                                  '"source_us":1}', '{"version":1,"source_id":"bad",'
                                  '"source_us":-1}'])
def test_invalid_focus_falls_back_to_first_episode(
    episode_project: tuple[Path, Project, RenderPlan], tmp_path: Path, saved: str,
) -> None:
    app = QApplication.instance() or QApplication([])
    root, _, _ = episode_project
    settings = QSettings(str(tmp_path / "view.ini"), QSettings.Format.IniFormat)
    parent = QWidget()
    first = EpisodePanel(root, parent, view_settings=settings)
    key = first.selection_key
    first.reject()
    settings.setValue(key, saved)
    panel = EpisodePanel(root, parent, view_settings=settings)
    try:
        assert panel.editor.currentRow() == panel.timeline.selected == 0
        assert panel.play_button.isEnabled()
    finally:
        panel.reject()
        parent.close()
        app.processEvents()


@pytest.mark.parametrize("language", ["ru", "en"])
@pytest.mark.parametrize("size", [(760, 540), (1000, 650)])
def test_compact_episode_window_gives_preview_space_and_aligns_controls(
    episode_project: tuple[Path, Project, RenderPlan], language: str, size: tuple[int, int],
) -> None:
    app = QApplication.instance() or QApplication([])
    set_language(language)
    parent = QWidget()
    panel = EpisodePanel(episode_project[0], parent)
    panel.resize(*size)
    panel.show()
    try:
        QTest.qWait(100)
        assert (panel.width(), panel.height()) == size
        assert panel.picture.height() >= panel.height() * .4
        assert panel.editor.height() <= 86
        assert not panel.message.text() and "\n" not in panel.summary.text()
        assert panel.summary.toolTip() and panel.help_button.toolTip()
        controls = (panel.previous_button, panel.play_button, panel.next_button,
                    panel.restart_button, panel.loop_button, panel.time_label,
                    panel.volume_control)
        centers = [c.mapTo(panel, c.rect().center()).y() for c in controls]
        assert max(centers) - min(centers) <= 1
        assert all(a.geometry().right() < b.geometry().left()
                   for a, b in zip(controls, controls[1:], strict=False))
        for control in (*controls, panel.sources, panel.export_button, panel.editor):
            assert panel.rect().contains(control.geometry())
        item = panel.editor.visualItemRect(panel.editor.item(0))
        assert panel.editor.viewport().rect().contains(item)
        assert not panel.undo_button.text() and panel.undo_button.accessibleName()
        assert panel.play_button.isEnabled() and panel.editor.currentRow() == 0
        assert panel.grab().save(str(episode_project[0].parent / f"episodes-{language}.png"))
    finally:
        panel.reject()
        parent.close()
        set_language("ru")
        app.processEvents()
