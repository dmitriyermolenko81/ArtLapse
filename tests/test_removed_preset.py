import pytest
from PySide6.QtWidgets import QApplication

from sculptcut.cli import build_parser
from sculptcut.domain import ProjectSettings
from sculptcut.shorts_layout import ACTIVE_SHORTS_PRESETS, RETIRED_STILL_PRESETS
from sculptcut.ui import MainWindow


def test_removed_preset_settings_remain_readable() -> None:
    legacy = {"preset": "locked_beat_shorts", "target_seconds": 60,
              "angle_reference": {"source_id": "old", "timestamp_us": 10},
              "angle_max_distance": 0.3}
    settings = ProjectSettings.model_validate(legacy)
    assert settings.preset == "beat_shorts"
    assert settings.target_seconds == 60
    assert "angle_reference" not in settings.model_dump()
    assert legacy["preset"] == "locked_beat_shorts"
    with pytest.raises(ValueError):
        ProjectSettings.model_validate(legacy | {"unknown_field": True})


def test_removed_preset_is_unavailable_in_cli() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["plan", "project", "--preset", "locked_beat_shorts"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["plan", "project", "--angle-seconds", "1"])


@pytest.mark.parametrize("preset", RETIRED_STILL_PRESETS)
def test_still_presets_cannot_be_selected_in_cli(preset: str) -> None:
    for command in ("plan", "run"):
        with pytest.raises(SystemExit):
            build_parser().parse_args([command, "project", "--preset", preset])


@pytest.mark.parametrize("preset", RETIRED_STILL_PRESETS)
def test_four_live_modes_and_legacy_session_recovery(
    preset: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    monkeypatch.setattr(window, "confirm_action", lambda *args: True)
    try:
        snapshot = ProjectSettings(preset=preset, target_seconds=60)
        draft = window.form_draft() | {"preset": preset, "target": 60}
        window.recent_settings.setValue("session_draft", json.dumps(draft))
        window._recover_after_crash = True
        window.recover_session()
        window.show()
        app.processEvents()
        assert not window.last_error
        assert window.preset.currentData() == RETIRED_STILL_PRESETS[preset]
        assert set(RETIRED_STILL_PRESETS).isdisjoint(window.preset.buttons)
        visible = {key for key, button in window.preset.buttons.items() if button.isVisible()}
        assert visible == set(ACTIVE_SHORTS_PRESETS)
        assert snapshot.preset == preset  # Saved plans keep the original interpretation.
    finally:
        window._allow_close = True
        window.close()
        app.processEvents()
