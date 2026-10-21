"""Structured logging and isolation tests."""

import io
import json
import logging
from datetime import datetime
from pathlib import Path

import pytest

from sculptcut.logging_config import configure_logging


def test_logging_is_structured_and_idempotent() -> None:
    stream = io.StringIO()
    root_handlers = logging.getLogger().handlers[:]
    configure_logging(stream)
    logger = configure_logging(stream)
    logger.info("Сессия %s", "готова", extra={"event": "test"})
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["message"] == "Сессия готова"
    assert record["level"] == "INFO"
    assert record["logger"] == "sculptcut"
    assert record["event"] == "test"
    assert datetime.fromisoformat(record["timestamp"]).utcoffset() is not None
    assert logging.getLogger().handlers == root_handlers


def test_exception_is_contained_in_one_json_line() -> None:
    stream = io.StringIO()
    logger = configure_logging(stream)
    try:
        raise ValueError("example")
    except ValueError:
        logger.exception("Operation failed")
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    assert "ValueError: example" in json.loads(lines[0])["exception"]


def test_unwritable_log_directory_does_not_prevent_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = tmp_path / "file-instead-of-directory"
    blocked.write_text("occupied", encoding="utf-8")
    monkeypatch.setenv("SCULPTCUT_DATA_DIR", str(blocked))
    stream = io.StringIO()
    logger = configure_logging(stream)
    logger.info("Application started")
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert records[0]["event"] == "log_file_unavailable"
    assert records[1]["message"] == "Application started"
