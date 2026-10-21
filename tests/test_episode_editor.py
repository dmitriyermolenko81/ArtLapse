from pathlib import Path

from PySide6.QtWidgets import QApplication, QMenu
from test_timeline import make_source

from sculptcut.domain import Override, Project
from sculptcut.episode_editor import EpisodeEditor, edit_episode
from sculptcut.overrides import apply_overrides
from sculptcut.planning import build_plan
from sculptcut.project import load_project, save_project


def test_replacing_edit_preserves_neighbours() -> None:
    project = Project(
        sources=[make_source(0, 0, 10_000_000)],
        overrides=[Override(source_id="0", start_us=0, end_us=10_000_000, action="DROP")],
    )
    plan = build_plan(project, [], 2)
    d = plan.decisions[0].model_copy(
        update={"source_start_us": 2_000_000, "source_end_us": 4_000_000}
    )
    revised = edit_episode(project, d, 2)
    assert [(o.start_us, o.end_us, o.action) for o in revised.overrides] == [
        (0, 2_000_000, "DROP"),
        (4_000_000, 10_000_000, "DROP"),
        (2_000_000, 4_000_000, "SET_SPEED"),
    ]
    result = apply_overrides(revised, plan)
    kept = [d for d in result.decisions if d.output_frame_count]
    assert len(kept) == 1 and kept[0].output_frame_count == 30


def test_menu_persists_slow_drop_and_restore(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    project = Project(sources=[make_source(0, 0, 10_000_000)])
    save_project(tmp_path, project)
    plan = build_plan(project, [], 2)
    (tmp_path / "render_plan.json").write_text(plan.model_dump_json(), encoding="utf-8")
    editor = EpisodeEditor()
    editor.load(tmp_path)
    errors: list[str] = []
    editor.failed.connect(errors.append)
    try:
        for action, expected in ((2, "SET_SPEED"), (1, "DROP"), (0, "SET_SPEED")):
            editor.choose(editor.item(0))
            menu = editor.findChildren(QMenu)[-1]
            menu.actions()[action].trigger()
            menu.close()
            saved = load_project(tmp_path)
            assert saved.overrides[0].action == expected
            if action == 2:
                assert saved.overrides[0].speed == 2.5
            editor.load(tmp_path)
            assert editor.count() > 0
        assert not errors
    finally:
        editor.close()
        app.processEvents()
