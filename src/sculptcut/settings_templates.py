"""Reusable form options, independent of project sources and manual edit history."""

import json
from typing import Literal
from uuid import uuid4

from pydantic import Field, FiniteFloat, field_validator, model_validator

from sculptcut.domain import Model, Roi
from sculptcut.i18n import tr


def preset_title(preset: str) -> str:
    return tr({
        "timelapse": "Таймлапс · эпизоды", "shorts": "Шортс · 9:16",
        "speed": "Просто ускорение · без вырезок",
        "music_shorts": "Шортс · под музыку · видеоэпизоды",
        "beat_shorts": "Шортс · под музыку · кадры",
        "photoshop_shorts": "Photoshop · 9:16",
        "photoshop_music_shorts": "Photoshop · под музыку · видеоэпизоды",
        "photoshop_beat_shorts": "Photoshop · под музыку · кадры",
    }[preset])


class TemplateOptions(Model):
    preset: Literal[
        "timelapse", "shorts", "beat_shorts", "music_shorts", "speed",
        "photoshop_shorts", "photoshop_beat_shorts", "photoshop_music_shorts",
    ]
    target_seconds: FiniteFloat = Field(ge=0)
    normal_minutes: FiniteFloat = Field(ge=0)
    shorts_seconds: FiniteFloat = Field(ge=0, le=300)
    frame_mode: Literal["full", "viewport"]
    crop_roi: Roi
    crop_customized: bool
    shorts_layout: Literal["auto", "square"]
    webcam_enabled: bool
    music: str
    music_selection: Literal["dynamic", "full_tracks"]
    intro: str
    presentation_seconds: FiniteFloat = Field(ge=0.5, le=60)
    presentation_position: Literal["start", "end"]

    @model_validator(mode="after")
    def valid_duration(self) -> "TemplateOptions":
        if self.preset not in ("timelapse", "speed") and self.target_seconds > 300:
            raise ValueError("Shorts duration exceeds 300 seconds")
        return self


class SettingsTemplate(Model):
    id: str = Field(default_factory=lambda: uuid4().hex, min_length=1)
    name: str
    options: TemplateOptions

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 80 or any(ord(char) < 32 for char in value):
            raise ValueError(tr("Введите название шаблона от 1 до 80 символов."))
        return value


class TemplateLibrary(Model):
    version: Literal[1] = 1
    templates: list[SettingsTemplate] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_templates(self) -> "TemplateLibrary":
        if (len({t.id for t in self.templates}) != len(self.templates)
                or len({t.name.casefold() for t in self.templates}) != len(self.templates)):
            raise ValueError("Duplicate settings template")
        return self


def read_templates(raw: str) -> TemplateLibrary:
    if not raw:
        return TemplateLibrary()
    try:
        return TemplateLibrary.model_validate(json.loads(raw))
    except (ValueError, TypeError) as exc:
        raise ValueError(tr(
            "Не удалось прочитать шаблоны. Сохранённые данные не изменены.",
        )) from exc
