import shutil
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QRect, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QListWidgetItem,
    QPushButton,
)

from sculptcut.analysis import extract_features
from sculptcut.media import discover, run_process
from sculptcut.project import create_project, fingerprint, load_project, save_project
from sculptcut.proxy import make_proxies, proxy_path
from sculptcut.rendering import verify_output
from sculptcut.ui import Job, MainWindow


def test_three_panel_layout_keeps_drop_zone_and_export_visible() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.resize(1280, 820)
        window.show()
        app.processEvents()
        assert window.main_splitter.count() == 3
        assert window.drop_zone.isVisible()
        assert window.sources.height() >= 180
        assert window.sidebar_scroll.verticalScrollBar().maximum() == 0
        for index in range(12):
            window.sources.addItem(QListWidgetItem(f"clip-{index:02d}.mp4"))
        app.processEvents()
        assert window.sources.verticalScrollBar().maximum() > 0
        setup_width, viewer_width, settings_width = window.main_splitter.sizes()
        assert 270 <= setup_width <= 350
        assert 300 <= settings_width <= 360
        assert viewer_width > setup_width
        assert window.setup_panel.width() <= window.sidebar_scroll.viewport().width()
        assert window.sources_panel.objectName() == "sourcesPanel"
        assert window.settings_panel.objectName() == "settingsPanel"
        sidebar_rect = window.sidebar_scroll.geometry()
        viewer_rect = window.viewer_scroll.geometry()
        settings_rect = window.settings_scroll.geometry()
        assert viewer_rect.left() - sidebar_rect.right() >= 10
        assert viewer_rect.top() == sidebar_rect.top()
        assert viewer_rect.bottom() == sidebar_rect.bottom()
        assert settings_rect.left() - viewer_rect.right() >= 10
        window.resize(1120, 700)
        app.processEvents()
        assert window.sidebar_scroll.horizontalScrollBar().maximum() == 0
        assert window.viewer_scroll.horizontalScrollBar().maximum() == 0
        assert window.settings_scroll.horizontalScrollBar().maximum() == 0
        assert window.create_button.isVisible()
        assert window.change_music_button.isVisible()
        assert window.change_intro_button.isVisible()
        assert window.outputs_button.isVisible()
        assert not window.progress_bar.isVisible()
        assert not window.cancel_button.isVisible()
    finally:
        window.close()
        app.processEvents()


def test_result_actions_are_visible_beside_the_export_controls() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.show()
        app.processEvents()
        assert window.preset.property("controlRole") == "presetButtons"
        assert window.variants.property("controlRole") == "menu"
        assert window.sources.dragDropMode() == QAbstractItemView.DragDropMode.DragDrop
        assert not hasattr(window, "more_button")
        assert not hasattr(window, "render_action")
        assert window.change_music_button.property("actionRole") == "secondary"
        assert window.change_intro_button.property("actionRole") == "secondary"
        assert window.outputs_button.property("actionRole") == "utility"
        for button in (
            window.change_music_button,
            window.change_intro_button,
            window.outputs_button,
        ):
            assert not button.icon().isNull()
            assert button.iconSize().width() >= 19
            icon_image = button.icon().pixmap(24, 24).toImage()
            assert any(
                icon_image.pixelColor(x, y).alpha() > 0
                for x in range(icon_image.width())
                for y in range(icon_image.height())
            )
        assert not hasattr(window, "aspect_wide")
        assert not hasattr(window, "aspect_vertical")
        assert not window.change_music_button.isEnabled()
        assert not window.change_intro_button.isEnabled()
        assert not window.outputs_button.isEnabled()
        assert window.cut_button.property("actionRole") == "disclosure"
        assert window.cut_button.text() == "Подрезка ролика"
        assert window.cut_button.accessibleName() == "Подрезка готового ролика"
        assert not window.cut_button.icon().isNull()
        assert "Открыть готовые видео" in window.outputs_button.text()
        save = next(
            button
            for button in window.output_editor.findChildren(QPushButton)
            if button.text() == "Сохранить копию"
        )
        assert save.objectName() == "primary"
    finally:
        window.close()
        app.processEvents()


def test_mode_switches_are_in_settings_header_and_still_switch_modes() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.resize(1120, 700)
        window.show()
        app.processEvents()
        panel_rect = window.settings_panel.rect()
        for button in (window.timelapse_mode, window.shorts_mode):
            button_rect = QRect(button.mapTo(window.settings_panel, QPoint()), button.size())
            assert button.parentWidget() is window.mode_tabs
            assert window.settings_panel.isAncestorOf(button)
            assert panel_rect.contains(button_rect)
            assert button_rect.top() < window.preset.geometry().top()

        QTest.mouseClick(window.shorts_mode, Qt.MouseButton.LeftButton)
        assert window.shorts_mode.isChecked()
        assert window.preset.currentData() == "shorts"
        assert window.mode_settings_title.text() == "Настройки Shorts"
        for control in (window.preset, window.duration_card, window.music, window.intro_image):
            assert window.mode_settings_panel.isAncestorOf(control)
        buttons = [b for b in window.preset.buttons.values() if b.isVisible()]
        assert len(buttons) == 6
        assert len({b.geometry().top() for b in buttons}) == 1
        assert all(not b.icon().isNull() for b in buttons)
        assert all(b.geometry().right() < window.preset.width() for b in buttons)
        for preset in ("music_shorts", "beat_shorts", "photoshop_shorts", "photoshop_beat_shorts",
                       "photoshop_music_shorts"):
            button = window.preset.buttons[preset]
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            app.processEvents()
            assert window.preset.currentData() == preset
            assert button.isChecked()
            assert sum(b.isChecked() for b in window.preset.buttons.values()) == 1
            if preset == "music_shorts":
                assert window.duration_in_seconds
                assert "пересобрать" in window.change_music_button.text()
                assert "видеоэпизоды" in button.toolTip()
        QTest.mouseClick(window.preset.buttons["shorts"], Qt.MouseButton.LeftButton)
        QTest.mouseClick(window.timelapse_mode, Qt.MouseButton.LeftButton)
        assert window.timelapse_mode.isChecked()
        assert window.preset.currentData() == "timelapse"
        assert window.mode_settings_title.text() == "Настройки таймлапса"
    finally:
        window.close()
        app.processEvents()


def test_top_mode_switch_controls_format_duration_and_crop() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.show()
        app.processEvents()
        assert window.timelapse_mode.isChecked()
        assert window.frame_mode.isVisible()
        window.preset.setCurrentIndex(window.preset.findData("speed"))
        assert "без удаления" in window.preset.buttons["speed"].toolTip()
        assert not hasattr(window, "preset_hint")
        window.shorts_mode.click()
        assert window.preset.currentData() == "shorts"
        assert window.duration_card.isVisible()
        assert not hasattr(window, "shorts_duration_choices")
        assert window.shorts_layout.isVisible()
        assert not window.frame_mode.isVisible()
        assert not window.duration_card.hours_container.isVisible()
        assert not hasattr(window.duration_card, "slider")
        window.duration_card.minutes.setValue(0)
        window.duration_card.seconds.setValue(30)
        window.duration_card.seconds.editingFinished.emit()
        assert window.target.value() == 30
        window.duration_card.minutes.setValue(6)
        window.duration_card.minutes.editingFinished.emit()
        assert window.target.value() == 300
        assert window.duration_card.seconds.value() == 0
        window.preset.setCurrentIndex(window.preset.findData("beat_shorts"))
        assert "музыки" in window.preset.buttons["beat_shorts"].toolTip()
        window.timelapse_mode.click()
        assert window.preset.currentData() == "speed"
        assert window.duration_card.isVisible()
        assert window.duration_card.hours_container.isVisible()
        window.target.setValue(125)
        assert window.target.value() == 125
        assert window.duration_card.hours.value() == 2
        assert window.duration_card.minutes.value() == 5
        window.shorts_mode.click()
        window.timelapse_mode.click()
        assert window.target.value() == 125
    finally:
        window.close()
        app.processEvents()


@pytest.mark.parametrize("name", ["", "   ", ".mp4"])
def test_create_requires_name_before_crop_or_job(
    tmp_path: Path, name: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()

    def unexpected_crop(path: Path) -> bool:
        pytest.fail("Empty name must block cropping")

    def unexpected_job(job: Job) -> None:
        pytest.fail("Empty name must block background work")

    monkeypatch.setattr(window, "choose_square_area", unexpected_crop)
    monkeypatch.setattr(Job, "start", unexpected_job)
    try:
        window.folder.setText(str(tmp_path))
        window.set_sources([tmp_path / "source.mp4"])
        window.preset.setCurrentIndex(window.preset.findData("shorts"))
        window.shorts_layout.setCurrentIndex(window.shorts_layout.findData("square"))
        window.output_name.setText(name)
        window.show()
        app.processEvents()
        window.create_button.click()
        assert window.job is None
        assert "имя файла" in window.last_error
        assert window.focusWidget() is window.output_name
        assert not (tmp_path / "project.json").exists()
    finally:
        window.close()
        app.processEvents()


def test_new_project_clears_previous_output_name(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.folder.setText(str(tmp_path / "old"))
        window.output_name.setText("Previous")
        window.open_project_folder(tmp_path / "new")
        assert window.output_name.text() == ""
    finally:
        window.close()
        app.processEvents()


def test_timelapse_frame_area_choice_is_visible_only_for_timelapse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    opened: list[bool] = []
    monkeypatch.setattr(window, "choose_crop_area", lambda: opened.append(True))
    try:
        window.show()
        app.processEvents()
        assert window.frame_mode.isVisible()
        assert window.frame_mode.currentData() == "full"
        window.frame_mode.buttons["viewport"].click()
        assert window.frame_mode.currentData() == "viewport"
        assert opened == [True]
        window.frame_mode.buttons["viewport"].click()
        assert opened == [True, True]
        assert not hasattr(window, "crop_button")
        window.preset.setCurrentIndex(window.preset.findData("shorts"))
        app.processEvents()
        assert not window.frame_mode.isVisible()
        assert not hasattr(window, "crop_button")
        window.preset.setCurrentIndex(window.preset.findData("timelapse"))
        app.processEvents()
        assert window.frame_mode.isVisible()
        assert window.frame_mode.currentData() == "viewport"
        assert opened == [True, True]
    finally:
        window.close()
        app.processEvents()


def test_timelapse_frame_area_restores_from_project(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=160x90:r=10:d=1",
            "-c:v",
            "libx264",
            str(source),
        ]
    )
    root = tmp_path / "project"
    project = create_project(root, [source])
    save_project(
        root,
        project.model_copy(
            update={"settings": project.settings.model_copy(update={"crop_mode": "viewport"})}
        ),
    )
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.show()
        window.open_project_folder(root)
        app.processEvents()
        assert window.preset.currentData() == "timelapse"
        assert window.frame_mode.currentData() == "viewport"
        assert not hasattr(window, "crop_button")
    finally:
        from sculptcut.source_cards import _active_source_loaders

        deadline = time.monotonic() + 5
        while _active_source_loaders and time.monotonic() < deadline:
            QTest.qWait(10)
        assert not _active_source_loaders
        window.close()
        app.processEvents()


def test_path_fields_open_pickers_without_separate_buttons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    music = tmp_path / "music"
    music.mkdir()
    intro = tmp_path / "intro.png"
    image = QPixmap(24, 24)
    image.fill()
    assert image.save(str(intro))
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(music))
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", lambda *args, **kwargs: (str(intro), "Images")
    )
    try:
        window.show()
        app.processEvents()
        buttons = {button.text() for button in window.findChildren(QPushButton)}
        assert "Папка" not in buttons
        assert "Файл" not in buttons
        assert window.music.isReadOnly()
        assert window.intro_image.isReadOnly()
        assert not window.music.clear_action.icon().isNull()
        QTest.mouseClick(window.music, Qt.MouseButton.LeftButton)
        QTest.mouseClick(window.intro_image, Qt.MouseButton.LeftButton)
        assert Path(window.music.text()) == music
        assert Path(window.intro_image.text()) == intro.resolve()
        assert window.intro_preview.pixmap() is not None
        assert not window.intro_preview.pixmap().isNull()
        window.music.clear_action.trigger()
        window.intro_image.clear_action.trigger()
        assert window.music.text() == ""
        assert window.intro_image.text() == ""
        pixmap = window.intro_preview.pixmap()
        assert pixmap is None or pixmap.isNull()
        assert window.viewer_context.text() == "ЕЩЁ НЕ СОЗДАН"
        assert "отключена" in window.status.text()
    finally:
        window.close()
        app.processEvents()


def test_result_actions_enable_after_export(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    project = tmp_path / "project"
    music = tmp_path / "music"
    (project / "renders").mkdir(parents=True)
    music.mkdir()
    intro = tmp_path / "intro.png"
    image = QPixmap(24, 24)
    image.fill()
    assert image.save(str(intro))
    try:
        window.show()
        window.folder.setText(str(project))
        window.music.setText(str(music))
        window.intro_image.setText(str(intro))
        assert window.change_music_button.isVisible()
        assert window.change_intro_button.isVisible()
        assert not window.change_music_button.isEnabled()
        assert not window.change_intro_button.isEnabled()
        (project / "renders" / "result.mp4").touch()
        window.update_result_actions()
        assert window.change_music_button.isEnabled()
        assert "Сменить музыку" in window.change_music_button.text()
        assert window.change_intro_button.isEnabled()
        assert "Обновить заставку" in window.change_intro_button.text()
        assert window.outputs_button.isEnabled()
        window.music.clear()
        assert window.change_music_button.isEnabled()
        window.intro_image.clear()
        assert window.change_intro_button.isEnabled()
        window.job = Job(lambda cancel, progress, stage: "")
        window.update_result_actions()
        assert not window.change_music_button.isEnabled()
        assert not window.change_intro_button.isEnabled()
        assert window.outputs_button.isEnabled()
        window.job = None
    finally:
        window.close()
        app.processEvents()


def test_visible_result_buttons_choose_missing_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    root = tmp_path / "project"
    (root / "renders").mkdir(parents=True)
    (root / "renders" / "finished.mp4").touch()
    music = tmp_path / "music"
    music.mkdir()
    intro = tmp_path / "intro.png"
    image = QPixmap(24, 24)
    image.fill()
    assert image.save(str(intro))
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(music))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(intro), ""))
    actions: list[str] = []
    monkeypatch.setattr(window, "another_music", lambda: actions.append("music"))
    monkeypatch.setattr(window, "another_intro", lambda: actions.append("intro"))
    try:
        window.show()
        window.folder.setText(str(root))
        assert window.change_music_button.isEnabled()
        assert window.change_intro_button.isEnabled()
        window.change_music_button.click()
        window.change_intro_button.click()
        assert window.music.text() == str(music)
        assert Path(window.intro_image.text()) == intro.resolve()
        assert actions == ["music", "intro"]
    finally:
        window.close()
        app.processEvents()


def test_edit_existing_sources_and_open_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b, c = [tmp_path / name for name in ("первый.mp4", "второй.mp4", "третий.mp4")]
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=160x120:r=15:d=4",
            "-c:v",
            "libx264",
            str(a),
        ]
    )
    shutil.copyfile(a, b)
    shutil.copyfile(a, c)
    original = {p: fingerprint(p) for p in (a, b, c)}
    root = tmp_path / "проект"
    project = create_project(root, [a, b])
    make_proxies(root, project)
    extract_features(root, project)
    retained_proxy = proxy_path(root, project, project.sources[1])
    retained_modified = retained_proxy.stat().st_mtime_ns
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    opened: list[QUrl] = []

    def open_url(url: QUrl) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(QDesktopServices, "openUrl", open_url)
    monkeypatch.setattr(
        QFileDialog, "getOpenFileNames", lambda *args, **kwargs: ([str(b), str(c)], "")
    )
    try:
        window.show()
        window.outputs_button.click()
        assert not opened
        window.folder.setText(str(root))
        window.set_sources([a, b])
        assert window.sources.item(0).text() == a.name
        assert window.sources.item(0).toolTip() == str(a)
        window.sources.setCurrentRow(0)
        window.remove_button.click()
        assert window.source_paths() == [b]
        window.add_videos()
        assert window.source_paths() == [b, c]  # No duplicate of the selected existing video.
        window.target.setValue(0.1)
        window.start_action("render")
        deadline = time.monotonic() + 90
        while window.job is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert window.job is None, "GUI operation did not complete"
        assert not window.last_error, window.last_error
        saved = load_project(root)
        assert [s.path for s in saved.sources] == [b, c]
        assert saved.sources[0].id == project.sources[1].id
        assert saved.sources[1].id not in {s.id for s in project.sources}
        assert saved.sources[1].global_start_us == saved.sources[0].duration_us
        assert retained_proxy.stat().st_mtime_ns == retained_modified
        output = root / "renders" / "timelapse-001_timelapse.mp4"
        verify_output(output, 180, 30)
        window.open_video(output, "Готовый ролик")
        frame_deadline = time.monotonic() + 5
        while window.video_frames_seen == 0 and time.monotonic() < frame_deadline:
            app.processEvents()
            time.sleep(0.01)
        assert window.video_frames_seen > 0
        app.processEvents()
        assert window.viewer_scroll.horizontalScrollBar().maximum() == 0
        window.outputs_button.click()
        assert Path(opened[-1].toLocalFile()) == root / "renders"
        window.sources.selectAll()
        window.remove_button.click()
        assert window.sources.count() == 0
        assert window.drop_zone.isVisible()
        assert Path(window.player.source().toLocalFile()) == output
        assert {p: fingerprint(p) for p in (a, b, c)} == original
        assert output.is_file()
    finally:
        window.close()
        app.processEvents()
