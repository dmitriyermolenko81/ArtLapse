"""Validation evidence must survive failure without overwriting earlier runs."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.validate import Validation


def test_validation_refuses_existing_evidence(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text("previous report", encoding="utf-8")
    with pytest.raises(FileExistsError):
        Validation(tmp_path)
    assert report.read_text(encoding="utf-8") == "previous report"
    assert list(tmp_path.iterdir()) == [report]


def test_real_failed_process_retains_diagnostic_and_stops(tmp_path: Path) -> None:
    validation = Validation(tmp_path / "проверка")
    with pytest.raises(RuntimeError, match="failed"):
        validation.run("failure", [sys.executable, "-c", "print('diagnostic'); exit(7)"])
    report = json.loads((validation.workspace / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "FAIL"
    assert report["steps"][0]["returncode"] == 7
    assert report["steps"][0]["status"] == "FAIL"
    assert "diagnostic" in (validation.workspace / "failure.log").read_text(encoding="utf-8")


def test_real_child_uses_isolated_state_and_unicode_paths(tmp_path: Path) -> None:
    validation = Validation(tmp_path / "проверка с пробелами")
    output = validation.run("environment", [sys.executable, "-c", (
        "import os; from pathlib import Path; "
        "root = Path(os.environ['SCULPTCUT_DATA_DIR']); "
        "root.mkdir(); (root / 'test.txt').write_text('isolated'); print(root)"
    )])
    assert str(validation.workspace / "data") in output
    assert (validation.workspace / "data/test.txt").read_text() == "isolated"
    report = json.loads((validation.workspace / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "RUNNING"  # A single successful step is not overall success.
    assert report["steps"][0]["status"] == "PASS"


def test_missing_executable_is_recorded_as_failure(tmp_path: Path) -> None:
    validation = Validation(tmp_path / "missing-tool")
    with pytest.raises(OSError):
        validation.run("missing", [str(tmp_path / "does-not-exist.exe")])
    report = json.loads((validation.workspace / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "FAIL"
    assert report["steps"][0]["status"] == "FAIL"


def test_real_timeout_retains_partial_diagnostic(tmp_path: Path) -> None:
    validation = Validation(tmp_path / "timeout")
    with pytest.raises(subprocess.TimeoutExpired):
        validation.run("timeout", [sys.executable, "-c", (
            "import time; print('started', flush=True); time.sleep(30)"
        )], timeout=1)
    report = json.loads((validation.workspace / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "FAIL"
    assert "timed out" in report["steps"][0]["error"]
    assert "started" in (validation.workspace / "timeout.log").read_text(encoding="utf-8")
