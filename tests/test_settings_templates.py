"""User templates persist on disk and apply without replacing project edits."""

import json
from pathlib import Path
from typing import get_args

import pytest
from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox
from test_episode_panel import episode_project as episode_project
from test_episode_panel import pulse_music, wait_until
from test_project_cleanup import project_root as project_root
from test_variant_deletion import wait_job

from sculptcut.domain import Project, ProjectSettings, Roi
from sculptcut.planning import RenderPlan
from sculptcut.project import load_project
from sculptcut.settings_templates import SettingsTemplate, TemplateLibrary, read_templates
from sculptcut.template_dialog import TEMPLATES_KEY, TemplateDialog
from sculptcut.ui import MainWindow

PRESETS = get_args(ProjectSettings.model_fields["preset"].annotation)


def open_templates(window: MainWindow, action) -> None:
    errors = []

    def interact() -> None:
        dialog = window.findChildren(TemplateDialog)[-1]
        try:
            assert dialog.isVisible()
            action(dialog)
        except Exception as exc:
            errors.append(repr(exc))
        finally:
            if dialog.isVisible():
                dialog.reject()

    QTimer.singleShot(0, interact)
    window.templates_button.click()
    assert not errors, errors


def confirmation(dialog: TemplateDialog, accept: bool, action) -> None:
    errors = []

    def choose() -> None:
        box = dialog.findChildren(QMessageBox)[-1]
        try:
            assert box.defaultButton() is box.button(QMessageBox.StandardButton.Cancel)
            button = (next(b for b in box.buttons()
                           if box.buttonRole(b) == QMessageBox.ButtonRole.AcceptRole)
                      if accept else box.button(QMessageBox.StandardButton.Cancel))
            button.click()
        except Exception as exc:
            errors.append(repr(exc))
            box.reject()

    QTimer.singleShot(0, choose)
    action()
    assert not errors, errors


def test_template_library_real_save_replace_cancel_delete_and_reopen(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    owner = MainWindow()
    ini = str(tmp_path / "templates.ini")
    settings = QSettings(ini, QSettings.Format.IniFormat)
    dialog = TemplateDialog(settings, owner.template_options, owner)
    dialog.show()
    try:
        assert not dialog.apply_button.isEnabled()
        dialog.save_button.click()
        assert dialog.creating and dialog.name.isVisible() and not dialog.items.isVisible()
        dialog.apply_button.click()
        assert not dialog.library.templates and "название" in dialog.message.text()
        assert dialog.apply_button.isEnabled()
        dialog.save_button.click()
        assert not dialog.creating and not dialog.apply_button.isEnabled()
        assert not dialog.name.text() and not dialog.message.text()
        dialog.save_button.click()
        dialog.name.setText("Мой таймлапс")
        QTest.keyClick(dialog.name, Qt.Key.Key_Return)
        original = dialog.library.templates[0]
        assert not dialog.name.text()
        assert not dialog.message.text() and not dialog.message.isVisible()
        assert dialog.items.currentIndex() == 0 and dialog.items.currentText() == original.name
        saved = str(settings.value(TEMPLATES_KEY))
        assert read_templates(saved).templates == [original]
        assert not dialog.creating and dialog.items.isVisible() and not dialog.name.isVisible()
        owner.target.setValue(2)
        dialog.save_button.click()
        dialog.name.setText("мой ТАЙМЛАПС")
        confirmation(dialog, False, dialog.apply_button.click)
        assert settings.value(TEMPLATES_KEY) == saved
        assert dialog.name.text() == "мой ТАЙМЛАПС" and dialog.creating
        confirmation(dialog, True, dialog.apply_button.click)
        replacement = dialog.library.templates[0]
        assert not dialog.name.text() and dialog.items.currentText() == replacement.name
        assert replacement.id == original.id and replacement.options.target_seconds == 120
        confirmation(dialog, False, dialog.delete_button.click)
        assert len(dialog.library.templates) == 1
        dialog.reject()
        reopened = TemplateDialog(QSettings(ini, QSettings.Format.IniFormat),
                                  owner.template_options, owner)
        reopened.show()
        assert reopened.library.templates == [replacement]
        assert reopened.items.currentIndex() == 0 and reopened.apply_button.isEnabled()
        assert not reopened.name.text()
        reopened.name.setText("Новый шаблон")
        reopened.selected(0)
        assert reopened.name.text() == "Новый шаблон"
        confirmation(reopened, True, reopened.delete_button.click)
        assert not reopened.library.templates and reopened.items.currentIndex() == -1
        assert not reopened.apply_button.isEnabled()
        assert not reopened.message.text() and not reopened.message.isVisible()
        reopened.reject()
    finally:
        dialog.reject()
        owner.close()
        app.processEvents()


@pytest.mark.parametrize("cancel", ["button", "escape"])
def test_real_toolbar_cancel_creation_keeps_selection_and_library(cancel: str) -> None:
    app = QApplication.instance() or QApplication([])
    owner = MainWindow()
    first = SettingsTemplate(name="First", options=owner.template_options())
    second = SettingsTemplate(name="Second", options=first.options)
    raw = TemplateLibrary(templates=[first, second]).model_dump_json()
    owner.recent_settings.setValue(TEMPLATES_KEY, raw)
    owner.show()

    def cancel_then_apply(dialog: TemplateDialog) -> None:
        dialog.items.setCurrentIndex(1)
        before = dialog.geometry()
        dialog.save_button.click()
        dialog.apply_button.click()
        assert dialog.creating and dialog.message.isVisible()
        dialog.name.setText("Discard this draft")
        if cancel == "button":
            dialog.save_button.click()
        else:
            QTest.keyClick(dialog.name, Qt.Key.Key_Escape)
        app.processEvents()
        assert dialog.isVisible() and not dialog.creating
        assert not dialog.name.text() and not dialog.name.isVisible()
        assert dialog.items.isVisible() and dialog.items.currentIndex() == 1
        assert dialog.apply_button.isEnabled() and dialog.delete_button.isEnabled()
        assert not dialog.message.text() and dialog.geometry() == before
        assert owner.recent_settings.value(TEMPLATES_KEY) == raw
        dialog.items.setCurrentIndex(0)
        dialog.apply_button.click()
        assert dialog.chosen == first

    try:
        open_templates(owner, cancel_then_apply)
        assert owner.recent_settings.value(TEMPLATES_KEY) == raw
        assert owner.preset.currentData() == first.options.preset
    finally:
        owner.close()
        app.processEvents()


@pytest.mark.parametrize("preset", PRESETS)
def test_real_toolbar_apply_all_modes_and_save_selected_options(
    episode_project: tuple[Path, Project, RenderPlan], preset: str, tmp_path: Path,
) -> None:
    app = QApplication.instance() or QApplication([])
    root, _, _ = episode_project
    owner = MainWindow()
    owner.open_project_folder(root)
    owner.show()
    try:
        music = pulse_music(tmp_path / "music.wav")
        image = QImage(96, 64, QImage.Format.Format_RGB32)
        image.fill(0xFF668899)
        intro = tmp_path / "intro.png"
        assert image.save(str(intro))
        owner.preset.setCurrentIndex(owner.preset.findData(preset))
        owner.target.setValue(3 if owner.duration_in_seconds else .05)
        owner.music.setText(str(music))
        owner.intro_image.setText(str(intro))
        owner.music_selection.setCurrentIndex(owner.music_selection.findData("full_tracks"))
        owner.crop_roi = Roi(x=.1, y=.2, width=.7, height=.6)
        owner.crop_customized = True
        owner.frame_mode.setCurrentIndex(owner.frame_mode.findData("viewport"))
        owner.shorts_layout.setCurrentIndex(owner.shorts_layout.findData("square"))
        expected = owner.template_options()
        template = SettingsTemplate(name="Пример", options=expected)
        owner.recent_settings.setValue(TEMPLATES_KEY, TemplateLibrary(
            templates=[template],
        ).model_dump_json())
        owner.preset.setCurrentIndex(owner.preset.findData("speed"))
        owner.target.setValue(1)
        owner.music.clear()
        owner.intro_image.clear()
        before = owner.template_options()
        sources = owner.listed_source_paths()
        data = (root / "project.json").read_bytes()
        plan = (root / "render_plan.json").read_bytes()
        open_templates(owner, lambda d: d.apply_button.click())
        assert owner.template_options() == expected
        assert owner.listed_source_paths() == sources
        assert owner.folder.text() == str(root)
        assert (root / "project.json").read_bytes() == data
        assert (root / "render_plan.json").read_bytes() == plan
        assert owner.edit_history.undo_label == "Шаблон настроек"
        owner.history_controller.undo()
        assert owner.template_options() == before
        owner.history_controller.redo()
        assert owner.template_options() == expected
        owner.save_button.click()
        wait_job(owner)
        assert not owner.last_error, owner.last_error
        settings = load_project(root).settings
        assert settings.preset == preset and settings.target_seconds == 3
        assert settings.music_path == music and settings.intro_image == intro
        assert settings.music_selection_mode == "full_tracks"
        assert settings.shorts_layout == "square"
    finally:
        if owner.job is not None:
            wait_job(owner)
        owner.close()
        app.processEvents()


def test_template_keeps_pending_cuts_source_rois_and_project_identity(project_root: Path) -> None:
    app = QApplication.instance() or QApplication([])
    owner = MainWindow()
    owner.show()
    owner.open_project_folder(project_root)
    try:
        wait_until(app, lambda: owner.player.duration() == 300)
        owner.toggle_output_editor()
        editor = owner.output_editor
        editor.start.setValue(.05)
        editor.end.setValue(.15)
        editor.add_cut()
        cuts = list(editor.cuts)
        options = owner.template_options().model_copy(update={
            "preset": "photoshop_music_shorts", "target_seconds": 2,
            "music": "missing-music.wav", "webcam_enabled": True,
        })
        template = SettingsTemplate(name="Shorts", options=options)
        owner.recent_settings.setValue(TEMPLATES_KEY, TemplateLibrary(
            templates=[template],
        ).model_dump_json())
        path = owner.listed_source_paths()[0]
        owner.webcam_rois[path] = Roi(x=.1, y=.1, width=.2, height=.2)
        owner.square_rois[path] = Roi(x=.2, y=.1, width=.5, height=.5)
        sources, video = owner.listed_source_paths(), owner.player.source()
        name = owner.output_name.text()
        project = (project_root / "project.json").read_bytes()
        open_templates(owner, lambda d: d.apply_button.click())
        assert editor.cuts == cuts and editor.isVisible() and owner.seek.maximum() == 200
        assert owner.player.source() == video and owner.listed_source_paths() == sources
        assert owner.output_name.text() == name and owner.webcam_rois[path].width == .2
        assert owner.square_rois[path].width == .5
        assert (project_root / "project.json").read_bytes() == project
        assert "недоступны" in owner.status.text()
        owner.history_controller.undo()
        assert editor.cuts == cuts
        owner.history_controller.undo()
        assert not editor.cuts
        owner.history_controller.redo()
        owner.history_controller.redo()
        assert editor.cuts == cuts and owner.preset.currentData() == "photoshop_music_shorts"
    finally:
        owner.close()
        app.processEvents()


@pytest.mark.parametrize("raw", ["invalid", '{"version":2,"templates":[]}',
                                 '{"version":1,"templates":[{}]}'])
def test_corrupt_template_library_is_not_overwritten(raw: str) -> None:
    app = QApplication.instance() or QApplication([])
    owner = MainWindow()
    owner.recent_settings.setValue(TEMPLATES_KEY, raw)
    dialog = TemplateDialog(owner.recent_settings, owner.template_options, owner)
    try:
        assert not dialog.readable and not dialog.save_button.isEnabled()
        assert not dialog.apply_button.isEnabled()
        dialog.name.setText("New")
        dialog.save_current()
        assert owner.recent_settings.value(TEMPLATES_KEY) == raw
        assert "не изменены" in dialog.message.text()
    finally:
        dialog.reject()
        owner.close()
        app.processEvents()


def test_write_failure_keeps_previous_template_library(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    owner = MainWindow()

    class FailingSettings(QSettings):
        def status(self):
            return QSettings.Status.AccessError

    settings = FailingSettings(str(tmp_path / "readonly.ini"), QSettings.Format.IniFormat)
    original = TemplateLibrary(templates=[SettingsTemplate(name="Old",
                                                         options=owner.template_options())])
    raw = original.model_dump_json()
    settings.setValue(TEMPLATES_KEY, raw)
    dialog = TemplateDialog(settings, owner.template_options, owner)
    try:
        dialog.save_button.click()
        dialog.name.setText("New")
        dialog.apply_button.click()
        assert dialog.creating
        assert settings.value(TEMPLATES_KEY) == raw and dialog.library == original
        assert "Не удалось сохранить" in dialog.message.text()
        assert dialog.name.text() == "New"
        assert json.loads(str(settings.value(TEMPLATES_KEY)))["templates"][0]["name"] == "Old"
    finally:
        dialog.reject()
        owner.close()
        app.processEvents()
