"""Public command-line contract tests."""

import subprocess
import sys

import pytest

from sculptcut.cli import build_parser, main

EXPECTED_COMMANDS = (
    "doctor",
    "probe",
    "create-project",
    "make-proxy",
    "set-roi",
    "analyze",
    "plan",
    "preview",
    "render",
    "run",
)


@pytest.mark.parametrize("command", EXPECTED_COMMANDS)
def test_command_help(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as result:
        main([command, "--help"])
    assert result.value.code == 0
    assert command in capsys.readouterr().out


@pytest.mark.parametrize("argv", [[], ["unknown"], ["render", "--unknown"]])
def test_invalid_arguments(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as result:
        main(argv)
    assert result.value.code == 2


def test_top_level_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as result:
        main(["--help"])
    assert result.value.code == 0
    output = capsys.readouterr().out
    assert all(command in output for command in EXPECTED_COMMANDS)


def test_module_entry_point() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "sculptcut", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "sculptcut 0.1.0" in result.stdout


def test_music_selection_option() -> None:
    args = build_parser().parse_args(["plan", "project", "--music-selection", "full_tracks"])
    assert args.music_selection == "full_tracks"
