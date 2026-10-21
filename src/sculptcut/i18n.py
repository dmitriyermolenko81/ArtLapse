"""Presentation-only Russian/English catalog; no Qt or project model dependency."""

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

Language = Literal["ru", "en"]
_language: Language = "ru"
CATALOG: dict[str, str] = json.loads(
    (Path(__file__).parent / "assets" / "en.json").read_text(encoding="utf-8"),
)
_REVERSE = {value: key for key, value in CATALOG.items()}
_FIELD = re.compile(r"\{(\d+)\}")


def language() -> Language:
    return _language


def set_language(value: str) -> None:
    global _language
    if value not in ("ru", "en"):
        raise ValueError("Unsupported interface language")
    _language = "en" if value == "en" else "ru"


def _patterns(catalog: dict[str, str]) -> list[tuple[re.Pattern[str], str]]:
    result = []
    for source, target in sorted(catalog.items(), key=lambda item: -len(item[0])):
        fields = list(_FIELD.finditer(source))
        if not fields:
            continue
        pieces: list[str] = []
        offset = 0
        for field in fields:
            pieces.extend(
                (re.escape(source[offset : field.start()]), f"(?P<v{field.group(1)}>.*?)")
            )
            offset = field.end()
        pieces.append(re.escape(source[offset:]))
        result.append((re.compile("".join(pieces), re.DOTALL), target))
    return result


_FORWARD_PATTERNS = _patterns(CATALOG)
_REVERSE_PATTERNS = _patterns(_REVERSE)


@lru_cache(maxsize=4096)
def translate(text: str, target: Language) -> str:
    """Translate known text/templates while preserving inserted names and paths."""
    catalog = CATALOG if target == "en" else _REVERSE
    if text in catalog:
        return catalog[text]
    patterns = _FORWARD_PATTERNS if target == "en" else _REVERSE_PATTERNS
    # Avoid pattern scans for already-English ordinary progress messages.
    if target == "en" and not re.search("[А-Яа-яЁё]", text):
        return text
    for pattern, template in patterns:
        match = pattern.fullmatch(text)
        if match is not None:
            values = match.groupdict()

            def insert(field: re.Match[str], values: dict[str, str] = values) -> str:
                return values[f"v{field.group(1)}"]

            return _FIELD.sub(insert, template)
    if "\n" in text:
        return "\n".join(translate(line, target) for line in text.split("\n"))
    # Some labels append a path/time to a translated prefix, rather than an f-string.
    for source in sorted(catalog, key=len, reverse=True):
        if len(source.strip()) > 3 and text.startswith(source):
            suffix = text[len(source):]
            if source.endswith(" ") or suffix.startswith((" · ", " (", ": ")):
                return catalog[source] + translate(suffix, target)
    return text


def tr(text: str) -> str:
    return translate(text, _language)
