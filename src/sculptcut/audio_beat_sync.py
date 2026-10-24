"""Cached, UI-independent beat analysis used to align Shorts edit points."""

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from sculptcut.media import Cancelled
from sculptcut.music_beats import beat_profile, foreground_accents
from sculptcut.project import atomic_write


@dataclass(frozen=True)
class MusicEvent:
    time_seconds: float
    strength: float


@dataclass(frozen=True)
class MusicAnalysis:
    tempo_bpm: float
    beats: tuple[MusicEvent, ...]
    onsets: tuple[MusicEvent, ...]


def analyze_music(
    path: Path,
    duration_seconds: float,
    cache_dir: Path,
    *,
    loop: bool = False,
    cancel: Event | None = None,
) -> MusicAnalysis:
    """Analyze one music file and cache results by path, size, and modification time."""
    if not path.is_file():
        raise ValueError(f"Music file does not exist: {path}")
    stat = path.stat()
    key_data = (
        f"continuous-beats-3|{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|"
        f"{duration_seconds:.6f}|{loop}"
    )
    key = hashlib.sha256(key_data.encode("utf-8")).hexdigest()
    cached_path = cache_dir / f"{key}.json"
    if cached_path.is_file():
        try:
            data = json.loads(cached_path.read_text(encoding="utf-8"))
            return MusicAnalysis(
                tempo_bpm=float(data["tempo_bpm"]),
                beats=tuple(MusicEvent(**event) for event in data["beats"]),
                onsets=tuple(MusicEvent(**event) for event in data["onsets"]),
            )
        except (OSError, ValueError, KeyError, TypeError):
            cached_path.unlink(missing_ok=True)
    if cancel and cancel.is_set():
        raise Cancelled("Music beat analysis cancelled")
    beats_raw = beat_profile(path, duration_seconds, loop, cancel)
    if cancel and cancel.is_set():
        raise Cancelled("Music beat analysis cancelled")
    onsets_raw = foreground_accents(path, duration_seconds, loop, cancel)
    beat_times = [time for time, _ in beats_raw]
    gaps = [right - left for left, right in zip(beat_times, beat_times[1:], strict=False)]
    tempo = 60 / sorted(gaps)[len(gaps) // 2] if gaps else 0.0
    analysis = MusicAnalysis(
        tempo_bpm=tempo,
        beats=tuple(MusicEvent(time, strength) for time, strength in beats_raw),
        onsets=tuple(MusicEvent(time, strength) for time, strength in onsets_raw),
    )
    atomic_write(
        cached_path,
        json.dumps(
            {
                "tempo_bpm": analysis.tempo_bpm,
                "beats": [event.__dict__ for event in analysis.beats],
                "onsets": [event.__dict__ for event in analysis.onsets],
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    return analysis


def select_cut_beats(
    analysis: MusicAnalysis,
    mode: str = "normal",
    min_interval: float = 0.4,
    max_interval: float = 2.5,
    *,
    phrase_min_interval: float | None = None,
    strength: float = 0.75,
) -> list[MusicEvent]:
    """Count local beats instead of comparing jittering times to a global BPM."""
    beats_per_cut = {"slow": 4, "normal": 2, "fast": 1}.get(mode)
    if beats_per_cut is None:
        raise ValueError(f"Unknown beat sync mode: {mode}")
    if min_interval <= 0 or max_interval < min_interval:
        raise ValueError("Invalid beat sync shot interval")
    if not analysis.beats:
        return []
    sampled: list[MusicEvent] = []
    next_index = 0
    for index, beat in enumerate(analysis.beats):
        if index < next_index:
            continue
        period = local_beat_period(analysis.beats, index)
        step = beats_per_cut
        if beat.strength < 0.45:
            step *= 2  # Quiet sections keep a still for whole numbers of beats.
        step = max(step, math.ceil(min_interval / period))
        while step > 1 and step * period > max_interval:
            step = max(1, step // 2)
        if sampled and beat.time_seconds - sampled[-1].time_seconds < min_interval:
            continue
        sampled.append(beat)
        next_index = index + step
    # Only rhythmically articulated phrases may supplement the main pulse.
    # Strength controls adherence to the pulse, never an off-beat time blend.
    phrase_events = rhythmic_phrase_events(analysis, strength)
    sampled.extend(phrase_events)
    sampled.sort(key=lambda event: event.time_seconds)
    if not sampled:
        return []
    selected: list[MusicEvent] = []
    for event in sampled:
        interval = (
            phrase_min_interval
            if phrase_min_interval is not None
            and (event in phrase_events or (selected and selected[-1] in phrase_events))
            else min_interval
        )
        if selected and event.time_seconds - selected[-1].time_seconds < interval:
            if event in phrase_events or (
                selected[-1] not in phrase_events and event.strength > selected[-1].strength
            ):
                selected[-1] = event
        else:
            selected.append(event)
    # Fill overlong gaps with the strongest available beat before the max hold.
    filled: list[MusicEvent] = []
    for event in selected:
        while filled and event.time_seconds - filled[-1].time_seconds > max_interval:
            cutoff = filled[-1].time_seconds + max_interval
            options = [
                beat
                for beat in analysis.beats
                if min_interval <= beat.time_seconds - filled[-1].time_seconds <= max_interval
                and beat.time_seconds < event.time_seconds
                and beat.strength >= 0.45
            ]
            if not options:
                break
            next_event = max(options, key=lambda beat: beat.time_seconds)
            if next_event.time_seconds > cutoff:
                break
            filled.append(next_event)
        filled.append(event)
    return filled


def local_beat_period(beats: tuple[MusicEvent, ...], index: int) -> float:
    section = beats[max(0, index - 3) : index + 4]
    gaps = sorted(
        right.time_seconds - left.time_seconds
        for left, right in zip(section, section[1:], strict=False)
        if right.time_seconds > left.time_seconds
    )
    return gaps[len(gaps) // 2] if gaps else 0.5


def rhythmic_phrase_events(analysis: MusicAnalysis, strength: float) -> list[MusicEvent]:
    """Reject irregular midrange runs that do not share the music's subdivision."""
    connected = connected_phrase_events(analysis.onsets)
    groups: list[list[MusicEvent]] = []
    for event in connected:
        if not groups or event.time_seconds - groups[-1][-1].time_seconds > 0.55:
            groups.append([])
        groups[-1].append(event)
    selected = []
    for group in groups:
        index = min(
            range(len(analysis.beats)),
            key=lambda i: abs(analysis.beats[i].time_seconds - group[0].time_seconds),
        )
        period = local_beat_period(analysis.beats, index)
        gaps = [b.time_seconds - a.time_seconds for a, b in zip(group, group[1:], strict=False)]
        if all(
            min(abs(beat.time_seconds - event.time_seconds) for beat in analysis.beats)
            <= period * 0.25
            for event in group
        ) and all(abs(gap / period - 1) <= 0.1 for gap in gaps):
            continue  # Articulation of the tracked pulse must not replace its attack time.
        # One shared subdivision for the entire phrase, rather than independently
        # accepting every loud syllable. Tighten the tolerance with sync strength.
        tolerance = 0.35 - 0.2 * strength
        if any(
            all(abs(gap / (period * subdivision) - 1) <= tolerance for gap in gaps)
            for subdivision in (0.25, 0.5, 1.0)
        ):
            selected.extend(group)
    return selected


def connected_phrase_events(onsets: tuple[MusicEvent, ...]) -> list[MusicEvent]:
    """Return only short connected accents, filtering isolated midrange tones."""
    groups: list[list[MusicEvent]] = []
    current: list[MusicEvent] = []
    for onset in onsets:
        if current and onset.time_seconds - current[-1].time_seconds > 0.55:
            groups.append(current)
            current = []
        current.append(onset)
    if current:
        groups.append(current)
    return [
        event
        for group in groups
        if len(group) >= 3 or (len(group) == 2 and all(event.strength >= 0.85 for event in group))
        for event in group
    ]
