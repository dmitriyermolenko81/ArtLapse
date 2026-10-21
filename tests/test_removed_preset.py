import pytest

from sculptcut.cli import build_parser
from sculptcut.domain import ProjectSettings


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
