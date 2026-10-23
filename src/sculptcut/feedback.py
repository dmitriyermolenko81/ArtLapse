"""Explicit user feedback; payloads contain no project files or automatic logs."""

import json
import platform
import re
import secrets
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field

from sculptcut import __version__
from sculptcut.domain import Model
from sculptcut.runtime_paths import environment_value, portable_root

MAX_MESSAGE = 2400
MAX_BODY_BYTES = 16_384
REPLY_TOKEN_PATTERN = r"^[A-Za-z0-9_-]{43}$"


class FeedbackContext(Model):
    version: str = Field(max_length=40)
    system: str = Field(max_length=80)
    mode: str = Field(max_length=40)


class Feedback(Model):
    schema_version: Literal[1] = 1
    id: UUID
    kind: Literal["bug", "idea", "other"]
    message: str = Field(min_length=10, max_length=MAX_MESSAGE)
    contact: str = Field(default="", max_length=150)
    context: FeedbackContext | None = None
    reply_token: str | None = Field(default=None, min_length=43, max_length=43,
                                   pattern=REPLY_TOKEN_PATTERN)


class FeedbackDraft(Feedback):
    message: str = Field(default="", max_length=MAX_MESSAGE)


def new_reply_token() -> str:
    return secrets.token_urlsafe(32)


def telegram_reply_url(token: str) -> str:
    if not re.fullmatch(REPLY_TOKEN_PATTERN, token):
        raise ValueError("Invalid Telegram reply token")
    return f"https://t.me/SculptCut_bot?start=r_{token}"


def technical_context(mode: str) -> FeedbackContext:
    return FeedbackContext(version=__version__,
                           system=f"{platform.system()} {platform.release()}", mode=mode)


def validate_endpoint(endpoint: str, *, allow_local_http: bool = False) -> str:
    parsed = urlsplit(endpoint)
    local = (allow_local_http and parsed.scheme == "http"
             and parsed.hostname in ("127.0.0.1", "localhost", "::1"))
    if ((parsed.scheme != "https" and not local) or not parsed.hostname
            or parsed.username or parsed.password or parsed.fragment or parsed.query
            or parsed.hostname == "api.telegram.org"):
        raise ValueError("Feedback endpoint must be an HTTPS URL without credentials or query")
    return endpoint


def feedback_endpoint() -> str:
    configured = (environment_value("FEEDBACK_ENDPOINT") or "").strip()
    if not configured:
        root = portable_root()
        config = (root or Path(__file__).resolve().parents[2]) / "feedback-config.json"
        if not config.is_file():
            config = Path(__file__).parent / "assets/feedback-config.json"
        if config.is_file():
            try:
                if config.stat().st_size > 4096:
                    return ""
                raw = config.read_text(encoding="utf-8")
                if len(raw) > 4096:
                    return ""
                value = json.loads(raw)
                configured = (value["endpoint"] if isinstance(value, dict)
                              and set(value) == {"endpoint"} else "")
            except (OSError, ValueError):
                return ""
    if not isinstance(configured, str) or not configured:
        return ""
    try:
        return validate_endpoint(configured)
    except ValueError:
        return ""


def report_text(report: Feedback) -> str:
    from sculptcut.i18n import tr

    category = tr({"bug": "Ошибка", "idea": "Предложение", "other": "Другое"}[report.kind])
    lines = [category, report.message]
    if report.contact:
        lines.append(tr("Контакт: ") + report.contact)
    if report.context:
        lines.append(tr(f"Версия: {report.context.version} · {report.context.system} · "
                        f"{report.context.mode}"))
    return "\n\n".join(lines)
