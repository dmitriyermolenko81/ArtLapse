"""Conservative idle ranges: ambiguous changes are retained."""

from collections.abc import Iterable
from pathlib import Path

from pydantic import Field

from sculptcut.analysis import FrameFeature, read_features
from sculptcut.domain import Model, Project
from sculptcut.project import atomic_write


class IdleSettings(Model):
    minimum_us: int = Field(default=5_000_000, gt=0)
    padding_us: int = Field(default=500_000, ge=0)
    max_change: float = Field(default=0.00002, ge=0)
    cursor_ratio: float = Field(default=0.001, ge=0, le=0.01)
    noise_signed_fraction: float = Field(default=0.02, ge=0)


DEFAULT_IDLE = IdleSettings()


class IdleRange(Model):
    source_id: str
    start_us: int
    end_us: int
    reason: str = "sustained_low_change"


def quiet(feature: FrameFeature, settings: IdleSettings, continuing: bool) -> bool:
    if feature.measurement_quality < 0.9:
        return False
    factor = 1.2 if continuing else 1.0
    exact = feature.roi_change <= settings.max_change * factor
    noise = (
        feature.noise > 0
        and abs(feature.signed_change) <= feature.noise * settings.noise_signed_fraction
        and feature.changed_pixel_ratio == 0
        and feature.histogram_change < 0.01
    )
    cursor = (
        0 < feature.changed_pixel_ratio <= settings.cursor_ratio
        and feature.histogram_change <= settings.cursor_ratio
        and abs(feature.signed_change) < settings.max_change
    )
    return exact or noise or cursor


def detect_idle(
    features: Iterable[FrameFeature],
    duration_us: int,
    step_us: int,
    settings: IdleSettings = DEFAULT_IDLE,
) -> list[IdleRange]:
    result: list[IdleRange] = []
    start: int | None = None
    last = -step_us
    source_id = ""

    def finish(end: int) -> None:
        if start is not None and end - start >= settings.minimum_us:
            padded_start = start + settings.padding_us
            padded_end = end - settings.padding_us
            if padded_start < padded_end:
                result.append(
                    IdleRange(source_id=source_id, start_us=padded_start, end_us=padded_end)
                )

    for feature in features:
        if source_id and feature.source_id != source_id:
            raise ValueError("Idle detection must not cross source boundaries")
        source_id = feature.source_id
        if feature.timestamp_us <= last:
            raise ValueError("Features must be ordered")
        if feature.timestamp_us - last > step_us * 1.5:
            finish(last + step_us)
        # First frame has no preceding evidence; preserve it.
        if feature.timestamp_us > 0 and quiet(feature, settings, start is not None):
            if start is None:
                start = feature.timestamp_us
        else:
            finish(feature.timestamp_us)
            start = None
        last = feature.timestamp_us
    finish(min(duration_us, last + step_us))
    return result


def project_idle(
    root: Path, project: Project, settings: IdleSettings = DEFAULT_IDLE
) -> list[IdleRange]:
    ranges = []
    for source in project.sources:
        ranges.extend(
            detect_idle(
                read_features(root, project, source),
                source.duration_us,
                round(1_000_000 / project.settings.proxy_fps),
                settings,
            )
        )
    atomic_write(
        root / "logs" / "idle.json", "[\n" + ",\n".join(r.model_dump_json() for r in ranges) + "\n]"
    )
    rows = "".join(
        f"<tr><td>{r.source_id}</td><td>{r.start_us / 1e6:.2f}</td>"
        f"<td>{r.end_us / 1e6:.2f}</td><td>{r.reason}</td></tr>"
        for r in ranges
    )
    atomic_write(
        root / "logs" / "idle.html",
        '<meta charset="utf-8"><h1>Idle candidates (source seconds)</h1><table>'
        + rows
        + "</table>",
    )
    return ranges
