"""Versioned persistent models. All media intervals are half-open microseconds."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sculptcut.output_naming import clean_output_name

DEFAULT_BEAT_SYNC_MODE: Literal["slow", "normal", "fast"] = "fast"
DEFAULT_BEAT_SYNC_STRENGTH = 1.0


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Roi(Model):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def bounds(self) -> "Roi":
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("ROI extends beyond image")
        return self


class CameraPoint(Model):
    time_us: int = Field(ge=0)
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float | None = Field(default=None, gt=0, le=32)
    height: float | None = Field(default=None, gt=0, le=32)


class SubjectCamera(Model):
    viewport: Roi
    fit_subject: bool = False
    blur_background: bool = False
    fill_width: bool = False  # Accept plans written by the later Shorts renderer.
    width: float = Field(gt=0, le=32)
    height: float = Field(gt=0, le=32)
    points: list[CameraPoint] = Field(min_length=1)
    background: int = Field(ge=0, le=255)
    background_rgb: tuple[int, int, int] | None = None

    @field_validator("background_rgb")
    @classmethod
    def valid_background_rgb(
        cls, value: tuple[int, int, int] | None,
    ) -> tuple[int, int, int] | None:
        if value is not None and any(channel < 0 or channel > 255 for channel in value):
            raise ValueError("Background channels must be between 0 and 255")
        return value

    @model_validator(mode="after")
    def ordered(self) -> "SubjectCamera":
        times = [p.time_us for p in self.points]
        if times != sorted(set(times)):
            raise ValueError("Camera timestamps must be strictly increasing")
        return self


class SourceMedia(Model):
    id: str
    path: Path
    duration_us: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps_num: int = Field(ge=0)
    fps_den: int = Field(gt=0)
    time_base: str
    start_time: int
    stream_index: int = Field(ge=0)
    rotation: int
    audio_streams: list[int]
    fingerprint: str
    fingerprint_mtime_ns: int | None = None
    location_fingerprint: str | None = None
    global_start_us: int = Field(ge=0)


class ProjectSettings(Model):
    @model_validator(mode="before")
    @classmethod
    def migrate_removed_preset(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        values = dict(value)
        if values.get("preset") == "locked_beat_shorts":
            values["preset"] = "beat_shorts"
        values.pop("angle_reference", None)
        values.pop("angle_max_distance", None)
        return values

    webcam_enabled: bool = False
    webcam_rois: dict[str, Roi | None] = Field(default_factory=dict)
    shorts_layout: Literal["auto", "square"] = "auto"
    shorts_square_rois: dict[str, Roi] = Field(default_factory=dict)
    square_tracking_fps: int = Field(default=5, ge=1, le=15)
    square_tracking_dead_zone: float = Field(default=0.08, ge=0, le=0.25)
    square_tracking_margin: float = Field(default=0.04, ge=0, le=0.15)
    sculpt_only: bool = False
    shorts_tracking_margin: float = Field(default=1.25, ge=1.1, le=2)
    shorts_subject_fill: float = Field(default=0.65, ge=0.3, le=0.9)
    shorts_horizontal_fill: float = Field(default=0.9, ge=0.75, le=1)
    shorts_extent_percentile: float = Field(default=0.9, ge=0.5, le=1)
    shorts_early_chapter_power: float = Field(default=2.0, ge=1, le=3)
    shorts_early_focus_min_seconds: int = Field(default=600, ge=0)
    proxy_width: int = Field(default=960, ge=16, le=3840)
    proxy_fps: int = Field(default=5, ge=1, le=60)
    target_seconds: float = Field(default=600, gt=0)
    output_fps: int = Field(default=30, ge=1, le=120)
    style: Literal["calm", "balanced", "dynamic"] = "balanced"
    music_path: Path | None = None
    music_folder: Path | None = None
    music_seed: int = Field(default=0, ge=0)
    music_selection_mode: Literal["dynamic", "full_tracks"] = "dynamic"
    music_loop: bool = True
    music_fade_seconds: float = Field(default=2, ge=0)
    beat_sync_enabled: bool = True
    beat_sync_mode: Literal["slow", "normal", "fast"] = DEFAULT_BEAT_SYNC_MODE
    beat_sync_strength: float = Field(default=DEFAULT_BEAT_SYNC_STRENGTH, ge=0, le=1)
    beat_sync_min_shot_seconds: float = Field(default=0.4, gt=0, le=2.5)
    beat_sync_max_shot_seconds: float = Field(default=2.5, ge=0.4, le=10)
    intro_image: Path | None = None
    presentation_video: Path | None = None
    presentation_seconds: float = Field(default=5, ge=0.5, le=60)
    presentation_position: Literal["start", "end"] = "start"
    presentation_source_index: int = Field(default=0, ge=0)
    intro_seconds: float = Field(default=2, ge=0.5, le=10)
    intro_transition_seconds: float = Field(default=0.5, ge=0.1, le=3)
    crop_mode: Literal["full", "canvas", "hybrid", "viewport"] = "full"
    timelapse_viewport_roi: Roi = Field(
        default_factory=lambda: Roi(x=0.10, y=0.12, width=0.75, height=0.75)
    )
    expressive_pacing: bool = True
    pacing_window_seconds: float = Field(default=60, ge=10, le=300)
    pacing_anchor_seconds: float = Field(default=4, ge=1, le=10)
    pacing_max_speed: float = Field(default=128, ge=32, le=256)
    pacing_low_change: float = Field(default=0.15, ge=0, le=1)
    timelapse_episode_source_seconds: int = Field(default=30, ge=5, le=180)
    timelapse_episode_output_seconds: int = Field(default=8, ge=2, le=30)
    timelapse_early_chapter_power: float = Field(default=2.0, ge=1, le=3)
    timelapse_early_focus_min_seconds: int = Field(default=600, ge=0)
    timelapse_episode_max_speed: int = Field(default=8, ge=1, le=64)
    timelapse_bridge_max_speed: int = Field(default=128, ge=8, le=512)
    timelapse_bridge_preferred_speed: int = Field(default=32, ge=8, le=128)
    timelapse_bridge_interval_seconds: int = Field(default=120, ge=30, le=600)
    timelapse_bridge_window_seconds: int = Field(default=12, ge=2, le=30)
    timelapse_bridge_min_output_seconds: float = Field(default=0.5, ge=0.1, le=2)
    timelapse_bridge_output_fraction: float = Field(default=0.15, ge=0, le=0.25)
    timelapse_bridge_speed_limit: int = Field(default=4, ge=1, le=8)
    timelapse_setup_search_seconds: float = Field(default=120, ge=0, le=300)
    timelapse_setup_work_seconds: float = Field(default=4, ge=1, le=30)
    timelapse_setup_context_seconds: float = Field(default=2, ge=0.5, le=10)
    timelapse_episode_limit: int = Field(default=120, ge=1, le=300)
    timelapse_static_cut_seconds: float = Field(default=2.0, gt=0, le=60)
    timelapse_static_min_score: float = Field(default=0.9, ge=0, le=1)
    timelapse_action_context_seconds: float = Field(default=1.0, ge=0, le=5)
    timelapse_outcome_hold_seconds: float = Field(default=0.6, ge=0, le=3)
    final_view_seconds: float = Field(default=10, ge=0, le=60)
    preset: Literal[
        "timelapse", "shorts", "beat_shorts", "music_shorts", "speed",
        "photoshop_shorts", "photoshop_beat_shorts", "photoshop_music_shorts",
    ] = "timelapse"
    output_name: str = ""

    @field_validator("output_name")
    @classmethod
    def valid_output_name(cls, value: str) -> str:
        return clean_output_name(value)

    @model_validator(mode="after")
    def music_source(self) -> "ProjectSettings":
        if self.music_path and self.music_folder:
            raise ValueError("Choose a music file or folder, not both")
        if self.beat_sync_min_shot_seconds > self.beat_sync_max_shot_seconds:
            raise ValueError("Beat sync minimum shot duration exceeds maximum")
        if self.intro_transition_seconds >= self.intro_seconds:
            raise ValueError("Intro transition must be shorter than the intro")
        return self


class Override(Model):
    source_id: str
    start_us: int = Field(ge=0)
    end_us: int = Field(gt=0)
    action: Literal["KEEP", "RETAIN", "DROP", "SET_SPEED", "LOCK_SEGMENT", "FORCE_MONTAGE"]
    speed: float = Field(default=1, ge=1)


class Project(Model):
    schema_version: Literal[1] = 1
    sources: list[SourceMedia]
    rois: dict[str, Roi] = Field(default_factory=dict)
    settings: ProjectSettings = Field(default_factory=ProjectSettings)
    overrides: list[Override] = Field(default_factory=list)

    @model_validator(mode="after")
    def timeline(self) -> "Project":
        position = 0
        ids: set[str] = set()
        for source in self.sources:
            if source.id in ids or source.global_start_us != position:
                raise ValueError("Sources must have unique IDs and a contiguous timeline")
            ids.add(source.id)
            position += source.duration_us
        if not set(self.rois).issubset(ids):
            raise ValueError("ROI references an unknown source")
        durations = {s.id: s.duration_us for s in self.sources}
        ends: dict[str, int] = {}
        for override in sorted(self.overrides, key=lambda o: (o.source_id, o.start_us)):
            if not (
                ends.get(override.source_id, 0)
                <= override.start_us
                < override.end_us
                <= durations.get(override.source_id, 0)
            ):
                raise ValueError("Overrides overlap or reference invalid source ranges")
            ends[override.source_id] = override.end_us
        return self
