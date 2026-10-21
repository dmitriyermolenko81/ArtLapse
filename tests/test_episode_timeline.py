from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from test_timeline import make_source

from sculptcut.domain import Project
from sculptcut.episode_editor import EpisodeEditor
from sculptcut.episode_timeline import EpisodeTimeline, trim_episode
from sculptcut.overrides import apply_overrides
from sculptcut.planning import build_plan
from sculptcut.project import load_project, save_project
from sculptcut.timeline_preview import PreviewSlider


def test_trim_preserves_outside_and_survives_reload(tmp_path: Path) -> None:
    project = Project(sources=[make_source(0, 0, 100_000_000)])
    plan = build_plan(project, [], 20)
    d = plan.decisions[0]
    project = trim_episode(project, d, 20_000_000, 40_000_000, 30)
    result = apply_overrides(project, plan)
    kept = [d for d in result.decisions if d.action != "DROP"]
    assert [(d.source_start_us, d.source_end_us) for d in kept] == [(20_000_000, 40_000_000)]
    project = trim_episode(project, kept[0], 10_000_000, 60_000_000, 30)
    save_project(tmp_path, project)
    result = apply_overrides(load_project(tmp_path), plan)
    assert [
        (d.source_start_us, d.source_end_us) for d in result.decisions if d.action != "DROP"
    ] == [(10_000_000, 60_000_000)]


def test_timeline_drag_mapping_and_source_switch(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    project = Project(sources=[make_source(0, 0, 100_000_000)])
    plan = build_plan(project, [], 20)
    project = trim_episode(project, plan.decisions[0], 20_000_000, 40_000_000, 30)
    save_project(tmp_path, project)
    (tmp_path / "render_plan.json").write_text(plan.model_dump_json(), encoding="utf-8")
    editor = EpisodeEditor()
    timeline = EpisodeTimeline(editor)
    slider = PreviewSlider()
    timeline.view_changed.connect(slider.setRange)
    timeline.resize(815, 66)
    slider.resize(815, 20)
    slider.setRange(0, 100_000)
    timeline.set_source(QUrl.fromLocalFile(str(project.sources[0].path.resolve())))
    errors: list[str] = []
    editor.failed.connect(errors.append)
    editor.load(tmp_path)
    timeline.show()
    app.processEvents()
    try:
        assert timeline.isVisible() and not editor.isVisible()
        for us in (0, 20_000_000, 40_000_000, 100_000_000):
            assert slider.time_at(timeline.x_at(us)) == us // 1000
        anchor = timeline.x_at(40_000_000)
        wheel = QWheelEvent(
            QPointF(anchor, 15),
            QPointF(anchor, 15),
            QPoint(),
            QPoint(0, 120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        QApplication.sendEvent(timeline, wheel)
        assert timeline.view_end - timeline.view_start < timeline.duration
        assert abs(timeline.time_at(anchor) - 40_000_000) < 2
        assert abs(slider.time_at(anchor) - 40_000) <= 1
        view = timeline.view_start, timeline.view_end
        editor.load(tmp_path)
        assert (timeline.view_start, timeline.view_end) == view
        QTest.mouseClick(
            timeline, Qt.MouseButton.LeftButton, pos=QPoint(round(timeline.x_at(30_000_000)), 15)
        )
        QTest.mousePress(
            timeline, Qt.MouseButton.LeftButton, pos=QPoint(round(timeline.x_at(40_000_000)), 15)
        )
        QTest.mouseMove(timeline, QPoint(round(timeline.x_at(60_000_000)), 15))
        QTest.mouseRelease(
            timeline, Qt.MouseButton.LeftButton, pos=QPoint(round(timeline.x_at(60_000_000)), 15)
        )
        assert not errors
        kept = [d for _, d in timeline.decisions() if d.action != "DROP"]
        assert len(kept) == 1
        assert abs(kept[0].source_end_us - 60_000_000) < 150_000
        assert (timeline.view_start, timeline.view_end) == view
        timeline.grab().save(str(tmp_path / "timeline.png"))
        timeline.set_source(QUrl.fromLocalFile(str(tmp_path / "export.mp4")))
        assert not timeline.isVisible()
    finally:
        slider.shutdown()
        timeline.close()
        editor.close()
        app.processEvents()


def test_zoom_limits_pan_and_reset() -> None:
    app = QApplication.instance() or QApplication([])
    editor = EpisodeEditor()
    timeline = EpisodeTimeline(editor)
    timeline.resize(815, 66)
    timeline.duration = 3_600_000_000
    timeline.set_view(0, timeline.duration)
    timeline.show()
    try:
        for delta in (12000, -12000):
            event = QWheelEvent(
                QPointF(407, 15),
                QPointF(407, 15),
                QPoint(),
                QPoint(0, delta),
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
                Qt.ScrollPhase.NoScrollPhase,
                False,
            )
            QApplication.sendEvent(timeline, event)
            assert 0 <= timeline.view_start < timeline.view_end <= timeline.duration
            assert timeline.view_end - timeline.view_start >= 1_000_000
        timeline.set_view(1_000_000_000, 100_000_000)
        QTest.mousePress(timeline, Qt.MouseButton.MiddleButton, pos=QPoint(400, 15))
        QTest.mouseMove(timeline, QPoint(480, 15))
        QTest.mouseRelease(timeline, Qt.MouseButton.MiddleButton, pos=QPoint(480, 15))
        assert timeline.view_start == 990_000_000
        QTest.mouseDClick(timeline, Qt.MouseButton.LeftButton, pos=QPoint(400, 50))
        assert (timeline.view_start, timeline.view_end) == (0, timeline.duration)
    finally:
        timeline.close()
        editor.close()
        app.processEvents()
