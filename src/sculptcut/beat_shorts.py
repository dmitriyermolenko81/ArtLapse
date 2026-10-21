"""Chronological still-frame Shorts whose cut times follow local music attacks."""

import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import cv2
import numpy as np
from numpy.typing import NDArray

from sculptcut.analysis import FrameFeature, read_features
from sculptcut.audio_beat_sync import analyze_music, select_cut_beats
from sculptcut.domain import CameraPoint, Project, Roi, SourceMedia, SubjectCamera
from sculptcut.intro import body_start_frame, output_frame_count
from sculptcut.media import Cancelled, MediaError
from sculptcut.music_beats import beat_profile, foreground_accents
from sculptcut.music_library import soundtrack
from sculptcut.planning import RenderDecision, RenderPlan, validate_plan
from sculptcut.proxy import proxy_path
from sculptcut.screen_cleanup import screen_kind
from sculptcut.segmentation import Segment
from sculptcut.shorts import canvas_bounds, locked_shorts_camera, sculpt_footprint, viewport_box
from sculptcut.subject_camera import camera_preview
from sculptcut.webcam import AnalysisCapture, webcam_roi


@dataclass(frozen=True)
class BeatStillSettings:
    minimum_cut_seconds: float = 0.35
    foreground_cut_seconds: float = 0.18
    foreground_beat_guard_seconds: float = 0.28
    foreground_phrase_gap_seconds: float = 0.55
    foreground_phrase_minimum_events: int = 3
    foreground_pair_minimum_strength: float = 0.85
    maximum_shots: int = 180
    candidates_per_slot: int = 24
    candidate_spacing_us: int = 5_000_000
    expanded_candidates_per_slot: int = 72
    expanded_candidate_spacing_us: int = 1_000_000
    minimum_quality: float = 0.8
    minimum_contrast: float = 0.025
    minimum_brightness: float = 0.03
    maximum_brightness: float = 0.97
    sample_interval_us: int = 200_000
    final_hold_seconds: float = 1.5
    quiet_hold_zoom_min_seconds: float = 0.7
    quiet_hold_zoom_max: float = 1.04
    still_camera_margin: float = 1.35
    uncertain_viewport_weight: float = 0.6
    wide_camera_weight: float = 0.7
    final_source_window_us: int = 300_000_000
    final_source_fraction: float = 0.1
    quiet_cut_multiplier: float = 5.0
    quiet_energy_curve_power: float = 3.0
    minimum_side_clearance: float = 0.035
    minimum_top_clearance: float = 0.035
    maximum_lower_edge_occupancy: float = 0.24
    maximum_vivid_interface_fraction: float = 0.005
    minimum_visible_change: float = 2.0
    maximum_hold_seconds: float = 4.0


SETTINGS = BeatStillSettings()


@dataclass(frozen=True)
class Candidate:
    source: SourceMedia
    feature: FrameFeature
    score: float


class BeatStillCandidatesInsufficient(ValueError):
    """The source does not provide enough distinct stills for a safe music grid."""


def _phrase_accents(accents: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Keep articulated phrases without letting isolated tones displace the beat."""
    if not accents:
        return []
    groups: list[list[tuple[float, float]]] = []
    current = [accents[0]]
    for accent in accents[1:]:
        if accent[0] - current[-1][0] > SETTINGS.foreground_phrase_gap_seconds:
            groups.append(current)
            current = []
        current.append(accent)
    groups.append(current)
    return [
        accent
        for group in groups
        if len(group) >= SETTINGS.foreground_phrase_minimum_events
        or (
            len(group) == 2
            and all(strength >= SETTINGS.foreground_pair_minimum_strength for _, strength in group)
        )
        for accent in group
    ]


def beat_boundaries(
    root: Path, project: Project, frame_count: int, cancel: Event | None
) -> list[int]:
    """Return body frame boundaries, accounting for an optional intro's audio offset."""
    if project.settings.music_path is None and project.settings.music_folder is None:
        raise ValueError("Шортс под музыку требует выбранный музыкальный файл или папку")
    fps = project.settings.output_fps
    offset = body_start_frame(project.settings, fps, frame_count)
    total_seconds = output_frame_count(project.settings, fps, frame_count) / fps
    music = soundtrack(root, project.settings, total_seconds, cancel)
    if music is None:
        raise ValueError("Музыка для ритмичного шортса не найдена")
    if project.settings.beat_sync_enabled and project.settings.beat_sync_strength > 0:
        analysis = analyze_music(
            music,
            total_seconds,
            root / "music_analysis_cache",
            loop=project.settings.music_loop and project.settings.music_folder is None,
            cancel=cancel,
        )
        cut_events = select_cut_beats(
            analysis,
            project.settings.beat_sync_mode,
            project.settings.beat_sync_min_shot_seconds,
            project.settings.beat_sync_max_shot_seconds,
            phrase_min_interval=SETTINGS.foreground_cut_seconds,
            strength=project.settings.beat_sync_strength,
        )
        final_hold = round(min(SETTINGS.final_hold_seconds, frame_count / fps * 0.2) * fps)
        minimum = max(1, round(SETTINGS.foreground_cut_seconds * fps))
        frames = sorted(
            {
                frame
                for event in cut_events
                if minimum <= (frame := round(event.time_seconds * fps) - offset)
                <= frame_count - final_hold
            }
        )
        if len(frames) < 2:
            raise ValueError("В музыке недостаточно отчётливых акцентов для покадровой нарезки")
        stride = max(1, math.ceil((len(frames) + 1) / SETTINGS.maximum_shots))
        return [0, *frames[::stride], frame_count]
    peaks = beat_profile(music, total_seconds, project.settings.music_loop, cancel)
    foreground = _phrase_accents(
        foreground_accents(music, total_seconds, project.settings.music_loop, cancel)
    )
    minimum = max(1, round(fps * SETTINGS.minimum_cut_seconds))
    foreground_minimum = max(1, round(fps * SETTINGS.foreground_cut_seconds))
    foreground_guard = max(1, round(fps * SETTINGS.foreground_beat_guard_seconds))
    final_hold = max(
        minimum, round(min(SETTINGS.final_hold_seconds, frame_count / fps * 0.2) * fps)
    )
    foreground_events = [
        (round(time * fps) - offset, strength, True) for time, strength in foreground
    ]
    beat_events = [
        (round(time * fps) - offset, energy, False) for time, energy in peaks
    ]
    events = sorted(
        foreground_events
        + [
            event
            for event in beat_events
            if not any(
                abs(event[0] - accent[0]) <= foreground_guard for accent in foreground_events
            )
        ],
        key=lambda event: (event[0], not event[2]),
    )
    selected: list[tuple[int, float, bool]] = []
    for frame, energy, is_foreground in events:
        quiet_energy = (1 - energy) ** SETTINGS.quiet_energy_curve_power
        cut_gap = (
            foreground_minimum
            if is_foreground
            else round(minimum * (1 + quiet_energy * (SETTINGS.quiet_cut_multiplier - 1)))
        )
        previous = selected[-1][0] if selected else 0
        if previous + cut_gap <= frame <= frame_count - final_hold:
            selected.append((frame, energy, is_foreground))
    if len(selected) < 2:
        raise ValueError("В музыке недостаточно отчётливых акцентов для покадровой нарезки")
    budget = SETTINGS.maximum_shots - 1
    if len(selected) > budget:
        foreground_cuts = [event for event in selected if event[2]]
        beat_cuts = [event for event in selected if not event[2]]
        if len(foreground_cuts) >= budget:
            stride = math.ceil(len(foreground_cuts) / budget)
            selected = foreground_cuts[::stride][:budget]
        else:
            remaining = budget - len(foreground_cuts)
            retained_beats = [
                beat_cuts[
                    min(
                        len(beat_cuts) - 1,
                        math.floor((index + 0.5) * len(beat_cuts) / remaining),
                    )
                ]
                for index in range(remaining)
            ]
            selected = sorted(foreground_cuts + retained_beats, key=lambda event: event[0])
    return [0, *(frame for frame, _, _ in selected), frame_count]


def _feature_score(feature: FrameFeature, segment: Segment | None) -> float:
    if (
        feature.measurement_quality < SETTINGS.minimum_quality
        or feature.contrast < SETTINGS.minimum_contrast
        or not SETTINGS.minimum_brightness <= feature.brightness <= SETTINGS.maximum_brightness
    ):
        return 0.0
    kind = segment.kind if segment else "UNKNOWN"
    if kind in ("PROCESSING", "INTERFACE", "CAMERA"):
        return 0.0
    kind_weight = {
        "SCULPT": 1.5,
        "FINAL_VIEW": 1.6,
        "MIXED": 0.9,
        "STATIC": 0.7,
        "UNKNOWN": 0.8,
    }.get(kind, 0.7)
    importance = segment.importance if segment else 0.5
    stability = 1 / (
        1 + 180 * feature.roi_change + 25 * feature.ui_change + 3 * feature.camera_score
    )
    return kind_weight * (0.5 + importance) * feature.contrast * stability


def _candidates(
    root: Path,
    project: Project,
    segments: list[Segment],
    slots: int,
    cancel: Event | None,
    progress: Callable[[float], None] | None,
    candidates_per_slot: int,
    candidate_spacing_us: int,
) -> list[list[Candidate]]:
    total = sum(source.duration_us for source in project.sources)
    final_window = min(
        SETTINGS.final_source_window_us, max(1, round(total * SETTINGS.final_source_fraction))
    )
    earlier_end = total - final_window
    choices: list[list[Candidate]] = [[] for _ in range(slots)]
    grouped: dict[str, list[Segment]] = {
        source.id: sorted(
            (s for s in segments if s.source_id == source.id), key=lambda s: s.start_us
        )
        for source in project.sources
    }
    for source in project.sources:
        rows = grouped[source.id]
        segment_index = 0
        for feature in read_features(root, project, source):
            if cancel and cancel.is_set():
                raise Cancelled("Beat-shorts frame selection cancelled")
            time_us = feature.timestamp_us
            if time_us >= source.duration_us or any(
                override.source_id == source.id
                and override.action == "DROP"
                and override.start_us <= time_us < override.end_us
                for override in project.overrides
            ):
                continue
            while segment_index < len(rows) and rows[segment_index].end_us <= time_us:
                segment_index += 1
            segment = (
                rows[segment_index]
                if segment_index < len(rows) and rows[segment_index].start_us <= time_us
                else None
            )
            score = _feature_score(feature, segment)
            if score <= 0:
                continue
            global_time = source.global_start_us + time_us
            slot = (
                slots - 1
                if global_time >= earlier_end
                else min(slots - 2, global_time * (slots - 1) // earlier_end)
            )
            ranked = choices[slot]
            nearby = next(
                (
                    existing
                    for existing in ranked
                    if existing.source.id == source.id
                    and abs(existing.feature.timestamp_us - time_us) < candidate_spacing_us
                ),
                None,
            )
            if nearby is not None:
                if nearby.score >= score:
                    continue
                ranked.remove(nearby)
            ranked.append(Candidate(source, feature, score))
            ranked.sort(key=lambda item: (-item.score, item.feature.timestamp_us))
            del ranked[candidates_per_slot:]
        if progress:
            progress(min(0.7, 0.7 * (source.global_start_us + source.duration_us) / total))
    return choices


def _camera(project: Project, candidate: Candidate, frame: NDArray[np.uint8]) -> SubjectCamera:
    source = candidate.source
    time_us = candidate.feature.timestamp_us
    height, width = frame.shape[:2]
    full = Roi(x=0, y=0, width=1, height=1)
    if source.rotation % 180 == 90:
        width, height = height, width
    native_portrait = abs(width / height - 9 / 16) <= 0.02
    if native_portrait:
        return SubjectCamera(
            viewport=full,
            width=1,
            height=1,
            points=[CameraPoint(time_us=time_us, x=0.5, y=0.5)],
            background=70,
        )
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.uint8)
    viewport = viewport_box(gray) or full
    subject = sculpt_footprint(gray, viewport)
    background = canvas_bounds(gray)
    color = round(background[-1]) if background else 70
    if subject is None:
        return SubjectCamera(
            viewport=viewport,
            fit_subject=True,
            width=viewport.width,
            height=viewport.width / ((9 / 16) * height / width),
            points=[
                CameraPoint(
                    time_us=time_us,
                    x=viewport.x + viewport.width / 2,
                    y=viewport.y + viewport.height / 2,
                )
            ],
            background=color,
        )
    return locked_shorts_camera(
        [(time_us, subject)],
        [subject],
        viewport,
        project.settings.output_fps,
        (9 / 16) * height / width,
        SETTINGS.still_camera_margin,
        color,
    )


def _complete_subject(frame: NDArray[np.uint8], viewport: Roi) -> bool:
    """Reject a still when the sculpt already runs out of the visible canvas."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.uint8)
    subject = sculpt_footprint(gray, viewport)
    if subject is None:
        return False
    height, width = gray.shape
    x = round(viewport.x * width)
    y = round(viewport.y * height)
    right = round((viewport.x + viewport.width) * width)
    bottom = round((viewport.y + viewport.height) * height)
    region = gray[y:bottom, x:right]
    color = frame[y:bottom, x:right]
    mode = int(np.bincount((region // 4).ravel(), minlength=64).argmax())
    background = float(np.median(region[region // 4 == mode]))
    lower_edge = float(np.mean(np.abs(region[-5:].astype(np.float32) - background) > 14))
    hsv = cv2.cvtColor(color, cv2.COLOR_BGR2HSV)
    vivid_interface = float(np.mean((hsv[:, :, 1] > 145) & (hsv[:, :, 2] > 105)))
    left = (subject.x - viewport.x) / viewport.width
    right = (viewport.x + viewport.width - subject.x - subject.width) / viewport.width
    top = (subject.y - viewport.y) / viewport.height
    return (
        left >= SETTINGS.minimum_side_clearance
        and right >= SETTINGS.minimum_side_clearance
        and top >= SETTINGS.minimum_top_clearance
        and lower_edge <= SETTINGS.maximum_lower_edge_occupancy
        and vivid_interface <= SETTINGS.maximum_vivid_interface_fraction
    )


def _visual_signature(
    frame: NDArray[np.uint8], camera: SubjectCamera, time_us: int
) -> NDArray[np.uint8]:
    preview = camera_preview(frame, camera, time_us, 90, 160)
    gray = cv2.cvtColor(preview, cv2.COLOR_BGR2GRAY)
    return cv2.resize(gray, (48, 80), interpolation=cv2.INTER_AREA).astype(np.uint8)


def _visual_distance(left: NDArray[np.uint8], right: NDArray[np.uint8]) -> float:
    return float(np.mean(np.abs(left.astype(np.int16) - right.astype(np.int16))))


def _distinct_indices(images: list[NDArray[np.uint8]], scores: list[float]) -> list[int]:
    """Keep the best frame from each consecutive run of near-identical views."""
    if not images:
        return []
    chosen = [0]
    for index in range(1, len(images)):
        chosen.append(index)
        while (
            len(chosen) >= 2
            and _visual_distance(images[chosen[-2]], images[chosen[-1]])
            < SETTINGS.minimum_visible_change
        ):
            if scores[chosen[-1]] > scores[chosen[-2]]:
                chosen.pop(-2)
            else:
                chosen.pop()
    return chosen


def _beat_still_plan(
    root: Path,
    project: Project,
    base: RenderPlan,
    segments: list[Segment],
    target_seconds: float,
    cancel: Event | None = None,
    progress: Callable[[float], None] | None = None,
    *,
    candidates_per_slot: int,
    candidate_spacing_us: int,
) -> RenderPlan:
    """Select source frames in order and assign each one a music-driven hold duration."""
    requested = round(target_seconds * base.output_fps)
    boundaries = beat_boundaries(root, project, requested, cancel)
    slots = len(boundaries) - 1
    choices = _candidates(
        root,
        project,
        segments,
        slots,
        cancel,
        progress,
        candidates_per_slot,
        candidate_spacing_us,
    )
    selected: list[tuple[int, Candidate, SubjectCamera]] = []
    usable: dict[tuple[str, int], tuple[float, Candidate, SubjectCamera]] = {}
    appearance: dict[tuple[str, int], NDArray[np.uint8]] = {}
    readers: dict[str, cv2.VideoCapture | AnalysisCapture] = {}
    try:
        for slot, ranked in enumerate(choices):
            if cancel and cancel.is_set():
                raise Cancelled("Beat-shorts frame selection cancelled")
            best: tuple[float, Candidate, SubjectCamera] | None = None
            for candidate in ranked:
                source = candidate.source
                reader = readers.get(source.id)
                if reader is None:
                    reader = AnalysisCapture(
                        proxy_path(root, project, source), webcam_roi(project.settings, source.id)
                    )
                    if not reader.isOpened():
                        raise MediaError(f"Cannot open proxy: {source.path}")
                    readers[source.id] = reader
                reader.set(cv2.CAP_PROP_POS_MSEC, candidate.feature.timestamp_us / 1000)
                ok, frame = reader.read()
                if not ok:
                    continue
                image = frame.astype(np.uint8)
                gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.uint8)
                if screen_kind(gray):
                    continue
                viewport = viewport_box(gray)
                if viewport is not None and not _complete_subject(image, viewport):
                    continue
                camera = _camera(project, candidate, image)
                reliability = 1.0 if viewport else SETTINGS.uncertain_viewport_weight
                if camera.width > camera.viewport.width * 0.75:
                    reliability *= SETTINGS.wide_camera_weight
                rank = candidate.score * reliability
                key = (source.id, candidate.feature.timestamp_us)
                usable[key] = (rank, candidate, camera)
                appearance[key] = _visual_signature(image, camera, candidate.feature.timestamp_us)
                if best is None or rank > best[0]:
                    best = (rank, candidate, camera)
            if best is not None:
                selected.append((slot, best[1], best[2]))
            if progress:
                progress(0.7 + 0.3 * (slot + 1) / slots)
    finally:
        for reader in readers.values():
            reader.release()
    used = {(candidate.source.id, candidate.feature.timestamp_us) for _, candidate, _ in selected}
    for slot in range(slots):
        if any(index == slot for index, _, _ in selected):
            continue
        previous = max(
            (candidate for index, candidate, _ in selected if index < slot),
            key=lambda candidate: candidate.source.global_start_us + candidate.feature.timestamp_us,
            default=None,
        )
        following = min(
            (candidate for index, candidate, _ in selected if index > slot),
            key=lambda candidate: candidate.source.global_start_us + candidate.feature.timestamp_us,
            default=None,
        )
        lower = (
            (previous.source.global_start_us + previous.feature.timestamp_us) if previous else -1
        )
        upper = (
            (following.source.global_start_us + following.feature.timestamp_us)
            if following
            else sum(source.duration_us for source in project.sources)
        )
        replacements = [
            (rank / (1 + 0.25 * abs(index - slot)), candidate, camera)
            for index, ranked in enumerate(choices)
            for entry in ranked
            if (match := usable.get((entry.source.id, entry.feature.timestamp_us))) is not None
            for rank, candidate, camera in (match,)
            if (candidate.source.id, candidate.feature.timestamp_us) not in used
            and lower < candidate.source.global_start_us + candidate.feature.timestamp_us < upper
        ]
        if replacements:
            _, candidate, camera = max(replacements, key=lambda item: item[0])
            selected.append((slot, candidate, camera))
            used.add((candidate.source.id, candidate.feature.timestamp_us))
    selected.sort(key=lambda item: item[0])
    repeated = any(
        _visual_distance(
            appearance[(left.source.id, left.feature.timestamp_us)],
            appearance[(right.source.id, right.feature.timestamp_us)],
        )
        < SETTINGS.minimum_visible_change
        for (_, left, _), (_, right, _) in zip(selected, selected[1:], strict=False)
    )
    if repeated or len(selected) < slots:
        # Repartition vetted, visually distinct frames over the musical grid.
        ordered_all = sorted(
            usable.values(),
            key=lambda item: item[1].source.global_start_us + item[1].feature.timestamp_us,
        )
        indices = _distinct_indices(
            [
                appearance[(candidate.source.id, candidate.feature.timestamp_us)]
                for _, candidate, _ in ordered_all
            ],
            [rank for rank, _, _ in ordered_all],
        )
        ordered = [ordered_all[index] for index in indices]
        if len(ordered) < 2:
            raise BeatStillCandidatesInsufficient(
                "Недостаточно визуально разных кадров модели для нарезки"
            )
        selected = []
        count = min(slots, len(ordered))
        for index in range(count):
            slot = index if count == slots else round(index * (slots - 1) / (count - 1))
            start = index * len(ordered) // count
            end = (index + 1) * len(ordered) // count
            _, candidate, camera = max(ordered[start:end], key=lambda item: item[0])
            selected.append((slot, candidate, camera))
    distinct = _distinct_indices(
        [
            appearance[(candidate.source.id, candidate.feature.timestamp_us)]
            for _, candidate, _ in selected
        ],
        [
            usable[(candidate.source.id, candidate.feature.timestamp_us)][0]
            for _, candidate, _ in selected
        ],
    )
    selected = [selected[index] for index in distinct]
    if len(selected) < 2:
        raise BeatStillCandidatesInsufficient(
            "Недостаточно пригодных кадров модели для нарезки под музыку"
        )
    selected.sort(key=lambda item: item[1].source.global_start_us + item[1].feature.timestamp_us)
    decisions: list[RenderDecision] = []
    chosen_index = 0
    for source in project.sources:
        position = 0
        while chosen_index < len(selected) and selected[chosen_index][1].source.id == source.id:
            index, candidate, camera = selected[chosen_index]
            timestamp = candidate.feature.timestamp_us
            if timestamp < position:
                chosen_index += 1
                continue
            if timestamp > position:
                decisions.append(
                    RenderDecision(
                        source_id=source.id,
                        source_start_us=position,
                        source_end_us=timestamp,
                        action="DROP",
                        output_frame_count=0,
                        provenance="auto:beat_omission",
                    )
                )
            next_slot = selected[chosen_index + 1][0] if chosen_index + 1 < len(selected) else slots
            start_frame = 0 if chosen_index == 0 else boundaries[index]
            frame_count = boundaries[next_slot] - start_frame
            if frame_count > round(SETTINGS.maximum_hold_seconds * base.output_fps):
                raise BeatStillCandidatesInsufficient(
                    "Недостаточно визуально разных кадров модели для ритмичной нарезки"
                )
            end = min(source.duration_us, timestamp + SETTINGS.sample_interval_us)
            for override in project.overrides:
                if (override.source_id == source.id and override.action == "DROP"
                        and timestamp < override.start_us < end):
                    end = override.start_us
            if (
                chosen_index + 1 < len(selected)
                and selected[chosen_index + 1][1].source.id == source.id
            ):
                end = min(end, selected[chosen_index + 1][1].feature.timestamp_us)
            decisions.append(
                RenderDecision(
                    source_id=source.id,
                    source_start_us=timestamp,
                    source_end_us=end,
                    action="HOLD",
                    output_frame_count=frame_count,
                    provenance="auto:beat_still",
                    subject_camera=camera,
                )
            )
            position = end
            chosen_index += 1
        if position < source.duration_us:
            decisions.append(
                RenderDecision(
                    source_id=source.id,
                    source_start_us=position,
                    source_end_us=source.duration_us,
                    action="DROP",
                    output_frame_count=0,
                    provenance="auto:beat_omission",
                )
            )
    result = base.model_copy(
        update={
            "decisions": decisions,
            "actual_frame_count": requested,
            "settings": base.settings
            | {
                "preset": "beat_shorts",
                "width": 1080,
                "height": 1920,
                "story": "beat_stills",
                "beat_sync": "on" if project.settings.beat_sync_enabled else "off",
                "beat_sync_mode": project.settings.beat_sync_mode,
                "quiet_hold_zoom_min_seconds": SETTINGS.quiet_hold_zoom_min_seconds,
                "quiet_hold_zoom_max": SETTINGS.quiet_hold_zoom_max,
            },
            "warnings": [
                "Musical attacks and selected stills are heuristic; review the finished rhythm"
            ],
        }
    )
    validate_plan(project, result)
    return result


def beat_still_plan(
    root: Path,
    project: Project,
    base: RenderPlan,
    segments: list[Segment],
    target_seconds: float,
    cancel: Event | None = None,
    progress: Callable[[float], None] | None = None,
) -> RenderPlan:
    """Search wider for distinct frames, then fall back to motion Shorts with music."""
    search_settings = (
        (SETTINGS.candidates_per_slot, SETTINGS.candidate_spacing_us),
        (SETTINGS.expanded_candidates_per_slot, SETTINGS.expanded_candidate_spacing_us),
    )
    for attempt, (limit, spacing) in enumerate(search_settings):
        try:
            return _beat_still_plan(
                root,
                project,
                base,
                segments,
                target_seconds,
                cancel,
                progress if attempt == 0 else None,
                candidates_per_slot=limit,
                candidate_spacing_us=spacing,
            )
        except BeatStillCandidatesInsufficient:
            if attempt == 0:
                continue
    from sculptcut.process_story import process_plan

    motion_project = project.model_copy(
        update={"settings": project.settings.model_copy(update={"preset": "shorts"})}
    )
    fallback = process_plan(root, motion_project, base, segments, cancel)
    result = fallback.model_copy(
        update={
            "settings": fallback.settings
            | {
                "preset": "beat_shorts",
                "story": "process",
                "beat_still_fallback": "motion",
                "width": 1080,
                "height": 1920,
            },
            "warnings": fallback.warnings
            + [
                "Insufficient distinct stills for music timing; "
                "used motion episodes with soundtrack."
            ],
        }
    )
    validate_plan(project, result)
    return result
