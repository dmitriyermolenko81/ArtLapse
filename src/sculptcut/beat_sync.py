"""Snap existing Beat Shorts still-frame boundaries toward music."""

import logging
from pathlib import Path
from threading import Event

from sculptcut.audio_beat_sync import (
    MusicEvent,
    analyze_music,
    connected_phrase_events,
    select_cut_beats,
)
from sculptcut.domain import Project
from sculptcut.intro import body_start_frame, output_frame_count
from sculptcut.music_library import soundtrack
from sculptcut.planning import RenderPlan, validate_plan


def snap_cuts_to_beats(
    original_cuts: list[float],
    beat_cuts: list[MusicEvent],
    tolerance: float = 0.30,
    strength: float = 0.75,
    min_interval: float = 0.4,
    max_interval: float = 2.5,
    duration: float | None = None,
    start_time: float = 0.0,
    alignment_events: list[MusicEvent] | None = None,
) -> list[float]:
    """Move existing cuts toward nearby beats while preserving legal shot lengths."""
    if not 0 <= strength <= 1 or tolerance < 0:
        raise ValueError("Invalid beat sync strength or tolerance")
    cuts = sorted(original_cuts)
    snapped: list[float] = []
    for index, original in enumerate(cuts):
        previous = snapped[-1] if snapped else start_time
        following = cuts[index + 1] if index + 1 < len(cuts) else duration
        candidates = [
            event
            for event in beat_cuts
            if abs(event.time_seconds - original) <= tolerance
        ]
        if not candidates or strength == 0:
            snapped.append(original)
            continue
        event = min(
            candidates,
            key=lambda candidate: (
                abs(candidate.time_seconds - original),
                -candidate.strength,
            ),
        )
        proposal = original + (event.time_seconds - original) * strength
        if alignment_events and min(
            abs(proposal - event.time_seconds) for event in alignment_events
        ) > min(abs(original - event.time_seconds) for event in alignment_events):
            snapped.append(original)
            continue
        before = proposal - previous
        after = (following - proposal) if following is not None else min_interval
        if before < min_interval or after < min_interval:
            snapped.append(original)
            continue
        if (
            before > max_interval
            or after > max_interval
            or (original - previous > max_interval and before > original - previous)
            or (
                following is not None
                and following - original > max_interval
                and after > following - original
            )
        ):
            snapped.append(original)
            continue
        snapped.append(proposal)
    return snapped


def apply_beat_sync(
    root: Path, project: Project, plan: RenderPlan, cancel: Event | None = None
) -> RenderPlan:
    """Apply optional music snapping to an existing Beat Shorts plan."""
    settings = project.settings
    if (
        not settings.beat_sync_enabled
        or settings.preset != "beat_shorts"
        or (settings.music_path is None and settings.music_folder is None)
    ):
        return plan
    fps = plan.output_fps
    music_offset = body_start_frame(settings, fps, plan.actual_frame_count) / fps
    duration = output_frame_count(settings, fps, plan.actual_frame_count) / fps
    track = soundtrack(root, settings, duration, cancel)
    if track is None:
        return plan
    analysis = analyze_music(
        track,
        duration,
        root / "music_analysis_cache",
        loop=settings.music_loop and settings.music_folder is None,
        cancel=cancel,
    )
    beat_cuts = select_cut_beats(
        analysis,
        settings.beat_sync_mode,
        settings.beat_sync_min_shot_seconds,
        settings.beat_sync_max_shot_seconds,
        strength=settings.beat_sync_strength,
    )
    alignment_events = [beat for beat in analysis.beats if beat.strength >= 0.45]
    alignment_events.extend(connected_phrase_events(analysis.onsets))
    active = [
        index for index, decision in enumerate(plan.decisions) if decision.output_frame_count > 0
    ]
    if len(active) < 2 or not beat_cuts:
        return plan
    original_cuts: list[float] = []
    frame = 0
    eligible: list[int] = []
    for cut_index, (index, next_index) in enumerate(zip(active, active[1:], strict=False)):
        decision = plan.decisions[index]
        frame += decision.output_frame_count
        next_decision = plan.decisions[next_index]
        original_cuts.append(frame / fps)
        fixed = ("user:KEEP", "user:SET_SPEED", "user:LOCK_SEGMENT")
        if (next_decision.provenance != "user:shorts_final"
                and decision.provenance not in fixed and next_decision.provenance not in fixed):
            eligible.append(cut_index)
    sync_times = [cut + music_offset for cut in original_cuts]
    snapped = snap_cuts_to_beats(
        sync_times,
        beat_cuts,
        strength=1.0 if settings.beat_sync_strength > 0 else 0.0,
        min_interval=settings.beat_sync_min_shot_seconds,
        max_interval=settings.beat_sync_max_shot_seconds,
        duration=duration,
        start_time=music_offset,
        alignment_events=alignment_events,
    )
    counts = [decision.output_frame_count for decision in plan.decisions]
    changes = 0
    for cut_index in eligible:
        delta = round((snapped[cut_index] - sync_times[cut_index]) * fps)
        if not delta:
            continue
        left_index, right_index = active[cut_index], active[cut_index + 1]
        left_count, right_count = counts[left_index] + delta, counts[right_index] - delta
        minimum = max(1, round(settings.beat_sync_min_shot_seconds * fps))
        maximum = max(minimum, round(settings.beat_sync_max_shot_seconds * fps))
        right_final = plan.decisions[right_index].provenance == "user:shorts_final"
        if not (minimum <= left_count <= maximum and minimum <= right_count):
            continue
        if not right_final and right_count > maximum:
            continue
        counts[left_index], counts[right_index] = left_count, right_count
        changes += 1
    if not changes:
        return plan
    result = plan.model_copy(
        update={
            "decisions": [
                decision.model_copy(update={"output_frame_count": counts[index]})
                if decision.output_frame_count
                else decision
                for index, decision in enumerate(plan.decisions)
            ],
            "settings": plan.settings
            | {"beat_sync": "on", "beat_sync_mode": settings.beat_sync_mode},
        }
    )
    validate_plan(project, result)
    logging.getLogger("artlapse").info(
        "Beat Sync adjusted Shorts edit points",
        extra={"event": "beat_sync", "adjusted_cuts": changes, "tempo_bpm": analysis.tempo_bpm},
    )
    return result
