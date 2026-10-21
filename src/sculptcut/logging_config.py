"""Application-scoped JSON logging to stderr."""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import TextIO

from sculptcut.runtime_paths import data_directory


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("event", "command", "status"):
            if key in record.__dict__:
                payload[key] = record.__dict__[key]
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(stream: TextIO | None = None) -> logging.Logger:
    """Configure the SculptCut logger without changing the root logger."""
    logger = logging.getLogger("sculptcut")
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    if stream is not None or sys.stderr is not None:
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    data = data_directory()
    if data is not None:
        directory = data / "logs"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                directory / f"sculptcut-{os.getpid()}.jsonl",
                maxBytes=2_000_000,
                backupCount=3,
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning(
                "File logging unavailable: %s", exc, extra={"event": "log_file_unavailable"}
            )
        else:
            file_handler.setFormatter(JsonFormatter())
            logger.addHandler(file_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger
