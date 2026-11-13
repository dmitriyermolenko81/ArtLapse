"""Pausing the experiment hides and blocks entry without disabling draft export."""

from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QApplication

from sculptcut.ui import MainWindow


def test_roll_stays_hidden_and_blocked_for_final_draft_shorts_and_language(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTLAPSE_DATA_DIR", str(tmp_path/"data"))
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    video = tmp_path/"existing.mp4"
    monkeypatch.setattr(window.player, "source", lambda: QUrl.fromLocalFile(str(video)))
    monkeypatch.setattr(window, "current_edit_video", lambda root: video)
    window.folder.setText(str(tmp_path))
    try:
        window.show()
        for preset, draft in (("timelapse", False), ("timelapse", True), ("shorts", True)):
            snapshot = {"project": {"settings": {"preset": preset}},
                        "plan": {"settings": {"draft": draft}}}
            monkeypatch.setattr("sculptcut.ui.read_variant",
                                lambda path, snapshot=snapshot: snapshot)
            window.playback_snapshot_path = None
            window.update_playback_controls()
            assert not window.roll_button.isVisible() and not window.roll_button.isEnabled()
            assert window.export_button.isEnabled() == draft
            assert window.play_button.isEnabled() and window.seek.isEnabled()
            window.show_episode_roll()
            assert window.roll_dialog is None and window.job is None
        for lang in ("en", "ru"):
            window.change_language(lang)
            window.update_playback_controls()
            assert not window.roll_button.isVisible() and not window.roll_button.isEnabled()
            assert window.export_button.isEnabled()
    finally:
        window._allow_close = True
        window.close()
        app.processEvents()
