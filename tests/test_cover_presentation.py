"""Selecting a presentation disables the cover; its settings stay available."""
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog
from test_episode_panel import wait_until
from test_intro_placement import media as media

from sculptcut.intro_dialog import IntroDialog
from sculptcut.presentation_dialog import PresentationDialog
from sculptcut.ui import MainWindow


@pytest.mark.parametrize('position', ['start', 'end'])
def test_presentation_clears_cover_but_user_can_reenable_it_in_settings(media, tmp_path,
                                                                      monkeypatch, position):
    body, clip, image = media
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    errors = []
    try:
        window.show()
        window.activateWindow()
        QTest.qWait(50)
        window.folder.setText(str(tmp_path/'project'))
        window.set_sources([body, clip])
        window.intro_image.setText(str(image))

        def presentation():
            dialog = window.findChildren(PresentationDialog)[-1]
            try:
                assert dialog.isVisible()
                dialog.video.setCurrentIndex(dialog.video.findData(str(clip)))
                dialog.position.setCurrentIndex(dialog.position.findData(position))
                dialog.apply_button.click()
            except Exception as exc:
                errors.append(str(exc))
                dialog.reject()

        QTimer.singleShot(30, presentation)
        window.presentation_button.click()
        assert not errors and not window.intro_image.text()
        assert window.intro_position == 'start'  # No automatic move to the end.
        assert window.intro_settings_button.isEnabled()
        assert not window.form_draft()['intro']
        monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *args: (str(image), ''))

        def intro():
            dialog = window.findChildren(IntroDialog)[-1]
            try:
                assert dialog.isVisible() and dialog.isModal()
                assert dialog.path is None
                dialog.browse_button.click()
                assert dialog.path == image
                dialog.seconds.setValue(1)
                screenshot = Path('artifacts/cover-presentation-313')/('settings-'+position+'.png')
                screenshot.parent.mkdir(parents=True, exist_ok=True)
                assert dialog.grab().save(str(screenshot))
                dialog.apply_button.click()
            except Exception as exc:
                errors.append(str(exc))
                dialog.reject()

        QTimer.singleShot(30, intro)
        window.intro_settings_button.click()
        assert not errors
        assert window.intro_image.text() == str(image)
        assert window.intro_position == ('end' if position == 'start' else 'start')
        assert window.presentation_video == clip.resolve()
    finally:
        window._allow_close = True
        window.close()
        from sculptcut.source_cards import _active_source_loaders

        wait_until(app, lambda: not _active_source_loaders)
        app.processEvents()
