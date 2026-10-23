"""Conservative cut alignment to local audio attacks, without changing source ranges."""

import math
import tempfile
from pathlib import Path
from threading import Event

import numpy as np

from sculptcut.domain import Project
from sculptcut.intro import body_start_frame, output_frame_count
from sculptcut.media import Cancelled, discover, run_process
from sculptcut.music_library import soundtrack
from sculptcut.planning import RenderPlan, validate_plan

RATE = 4000
HOP = 80
MAX_SHIFT_SECONDS = 0.18
MAX_DURATION_CHANGE = 0.10


def accent_profile(
    path: Path, seconds: float, loop: bool, cancel: Event | None
) -> list[tuple[float, float]]:
    """Return attack times and their energy relative to this soundtrack."""
    with tempfile.TemporaryDirectory(prefix="sculptcut-rhythm-") as folder:
        pcm = Path(folder) / "audio.f32"
        run_process(
            [
                discover("ffmpeg"),
                "-v",
                "error",
                "-nostdin",
                *(["-stream_loop", "-1"] if loop else []),
                "-i",
                str(path),
                "-t",
                str(seconds),
                "-map",
                "0:a:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(RATE),
                "-f",
                "f32le",
                str(pcm),
            ],
            cancel,
        )
        energy: list[float] = []
        with pcm.open("rb") as stream:
            while block := stream.read(HOP * 4 * 500):
                if cancel and cancel.is_set():
                    raise Cancelled("Rhythm analysis cancelled")
                samples = np.frombuffer(block, dtype="<f4")
                n = len(samples) // HOP
                if n:
                    energy.extend(
                        np.sqrt(np.mean(samples[: n * HOP].reshape(n, HOP) ** 2, axis=1)).tolist()
                    )
    if len(energy) < 3:
        return []
    envelope = np.asarray(energy)
    attack = np.maximum(0, np.diff(envelope, prepend=envelope[0]))
    threshold = max(float(np.quantile(attack, 0.90)), float(np.max(attack)) * 0.12, 0.0001)
    peaks = [
        i
        for i in range(1, len(attack) - 1)
        if attack[i] >= threshold and attack[i] > attack[i - 1] and attack[i] >= attack[i + 1]
    ]
    selected: list[int] = []
    for i in peaks:
        if selected and i - selected[-1] < 8:
            if attack[i] > attack[selected[-1]]:
                selected[-1] = i
        else:
            selected.append(i)
    reference = max(float(np.max(envelope)), 0.0001)
    return [(i * HOP / RATE, min(1.0, float(envelope[i]) / reference)) for i in selected]


def accents(path: Path, seconds: float, loop: bool, cancel: Event | None) -> list[float]:
    return [time for time, _ in accent_profile(path, seconds, loop, cancel)]


def align_plan(root: Path, project: Project, plan: RenderPlan, cancel: Event | None) -> RenderPlan:
    duration = output_frame_count(project.settings, plan.output_fps, plan.actual_frame_count)
    offset = body_start_frame(project.settings, plan.output_fps, plan.actual_frame_count)
    music = soundtrack(root, project.settings, duration / plan.output_fps, cancel)
    if music is None:
        return plan
    peaks = accents(
        music, duration / plan.output_fps, project.settings.music_loop, cancel
    )
    fps = plan.output_fps
    beat_frames = sorted(
        set(
            round(t * fps) - offset
            for t in peaks
            if project.settings.music_fade_seconds
            < t - offset / fps
            < plan.actual_frame_count / fps - project.settings.music_fade_seconds
        )
    )
    decisions = list(plan.decisions)
    kept = [i for i, d in enumerate(decisions) if d.output_frame_count]
    original = [d.output_frame_count for d in decisions]
    position = 0
    moved = 0

    def bounds(index: int) -> tuple[int, int]:
        d = decisions[index]
        natural = (d.source_end_us - d.source_start_us) * fps / 1e6
        old = original[index]
        # Do not make already-fast legacy plans faster or slow footage below 1x.
        minimum = max(
            math.ceil(old * (1 - MAX_DURATION_CHANGE)),
            math.ceil(natural / max(8, natural / old)),
            min(fps, old),
        )
        maximum = min(math.floor(natural), math.floor(old * (1 + MAX_DURATION_CHANGE)))
        return minimum, maximum

    for left, right in zip(kept, kept[1:], strict=False):
        position += decisions[left].output_frame_count
        a, b = decisions[left], decisions[right]
        if any(
            d.action != "SPEED"
            or (d.provenance.startswith("user:") and d.provenance != "user:process_story")
            for d in (a, b)
        ):
            continue
        low_a, high_a = bounds(left)
        low_b, high_b = bounds(right)
        candidates = [
            beat - position
            for beat in beat_frames
            if abs(beat - position) <= round(MAX_SHIFT_SECONDS * fps)
        ]
        candidates.sort(key=lambda shift: (abs(shift), shift))
        shift = next(
            (
                s
                for s in candidates
                if low_a <= a.output_frame_count + s <= high_a
                and low_b <= b.output_frame_count - s <= high_b
            ),
            0,
        )
        if shift:
            decisions[left] = a.model_copy(
                update={"output_frame_count": a.output_frame_count + shift}
            )
            decisions[right] = b.model_copy(
                update={"output_frame_count": b.output_frame_count - shift}
            )
            position += shift
            moved += 1
    result = plan.model_copy(
        update={"decisions": decisions, "settings": plan.settings | {"rhythm_cuts": moved}}
    )
    validate_plan(project, result)
    return result
