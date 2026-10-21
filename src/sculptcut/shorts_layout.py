"""Fixed source squares centered in portrait delivery frames."""

import tempfile
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

from sculptcut.domain import Project, ProjectSettings, Roi

if TYPE_CHECKING:
    from sculptcut.planning import RenderPlan

SQUARE_BACKGROUND = "0x181818"
SHORTS_PRESETS = (
    "shorts", "music_shorts", "beat_shorts", "photoshop_shorts", "photoshop_beat_shorts",
)
MUSIC_SHORTS_PRESETS = ("music_shorts", "beat_shorts", "photoshop_beat_shorts")


def uses_square(settings: ProjectSettings) -> bool:
    return settings.preset in SHORTS_PRESETS and settings.shorts_layout == "square"


def square_composition(width: int, height: int) -> str:
    """Fit an already cropped square, then add equally sized solid bands."""
    side = min(width, height)
    return (
        f"scale={side}:{side}:flags=lanczos,setsar=1,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={SQUARE_BACKGROUND},"
    )


def apply_square_layout(
    project: Project,
    plan: "RenderPlan",
    root: Path | None = None,
    cancel: Event | None = None,
) -> "RenderPlan":
    from sculptcut.cropping import fit_video_aspect

    if not uses_square(project.settings):
        return plan
    crops = {}
    for source in project.sources:
        roi = project.settings.shorts_square_rois.get(source.id)
        if roi is None:
            raise ValueError(f"Set a square crop for source: {source.path.name}")
        width, height = source.width, source.height
        if source.rotation % 180 == 90:
            width, height = height, width
        crops[source.id] = fit_video_aspect(roi, width, height, 1)
    decisions = [
        d.model_copy(update={"crop": crops[d.source_id], "subject_camera": None})
        for d in plan.decisions
    ]
    warnings = plan.warnings[:]
    if root is not None:
        from sculptcut.square_tracking import (
            horizontal_camera,
            measure_square_episode,
            safe_viewport,
        )

        sources = {source.id: source for source in project.sources}
        last_center: dict[str, float] = {}
        with tempfile.TemporaryDirectory(prefix="square-camera-", dir=root) as directory:
            for index, decision in enumerate(decisions):
                if decision.action == "DROP":
                    continue
                roi = crops[decision.source_id]
                viewports: list[Roi] = []
                samples = measure_square_episode(
                    sources[decision.source_id],
                    roi,
                    decision.source_start_us,
                    decision.source_end_us,
                    decision.output_frame_count,
                    plan.output_fps,
                    decision.action == "HOLD",
                    project.settings,
                    Path(directory) / "episode.mp4",
                    cancel,
                    viewports,
                    project.rois.get(decision.source_id),
                )
                if not samples:
                    warning = (
                        "Square camera: no reliable subject; keep selected frame: "
                        f"{decision.source_id}"
                    )
                    if warning not in warnings:
                        warnings.append(warning)
                elif any(box.width > roi.width for _, box in samples):
                    warning = (
                        f"Square camera: subject wider than selected square: {decision.source_id}"
                    )
                    if warning not in warnings:
                        warnings.append(warning)
                camera = horizontal_camera(
                    roi,
                    samples,
                    project.settings,
                    decision.source_start_us,
                    last_center.get(decision.source_id),
                    safe_viewport(viewports, roi),
                )
                last_center[decision.source_id] = camera.points[-1].x
                decisions[index] = decision.model_copy(update={"subject_camera": camera})
    return plan.model_copy(
        update={
            "decisions": decisions,
            "warnings": warnings,
            "settings": plan.settings
            | {
                "shorts_layout": "square",
                "square_background": SQUARE_BACKGROUND,
                "bookends": plan.settings.get("bookends", False),
                "quiet_hold_zoom_max": 1.0,
            },
        }
    )
