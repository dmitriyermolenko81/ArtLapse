"""The same continuous source/output clock for export, preview and manual cuts."""

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sculptcut.planning import RenderDecision


def _value(decision: "RenderDecision", time_us: float) -> float:
    ramp = decision.speed_ramp
    if ramp is None or decision.action not in ("SPEED", "KEEP"):
        return time_us
    u = (time_us - ramp.start_us) / (ramp.end_us - ramp.start_us)
    return u - ramp.strength * math.sin(2 * math.pi * u) / (2 * math.pi)


def output_fraction(decision: "RenderDecision", time_us: int) -> float:
    a, b = _value(decision, decision.source_start_us), _value(decision, decision.source_end_us)
    return max(0.0, min(1.0, (_value(decision, time_us) - a) / (b - a)))


def source_time(decision: "RenderDecision", fraction: float) -> int:
    if fraction <= 0:
        return decision.source_start_us
    if fraction >= 1:
        return decision.source_end_us
    if decision.speed_ramp is None:
        return decision.source_start_us + int(
            (decision.source_end_us - decision.source_start_us) * fraction
        )
    lo, hi = decision.source_start_us, decision.source_end_us
    for _ in range(48):
        mid = (lo + hi) // 2
        if output_fraction(decision, mid) < fraction:
            lo = mid + 1
        else:
            hi = mid
    return hi


def output_before(decision: "RenderDecision", time_us: int) -> int:
    if decision.speed_ramp is None:
        # A video-frame timestamp rounded to integer microseconds can be <1us
        # before the mathematical boundary. Preserve that frame when reusing cuts.
        return max(0, min(decision.output_frame_count, (
            (time_us - decision.source_start_us + 1)
            * decision.output_frame_count
            // (decision.source_end_us - decision.source_start_us)
        )))
    return round(output_fraction(decision, time_us) * decision.output_frame_count)


def pts_expression(decision: "RenderDecision", fps: int) -> str:
    ramp = decision.speed_ramp
    if ramp is None:
        ratio = (
            decision.output_frame_count
            / fps
            / ((decision.source_end_us - decision.source_start_us) / 1e6)
        )
        return f"PTS*{ratio:.12f}"
    length = (ramp.end_us - ramp.start_us) / 1e6
    offset = (decision.source_start_us - ramp.start_us) / 1e6
    a, b = _value(decision, decision.source_start_us), _value(decision, decision.source_end_us)
    u = f"((PTS*TB+{offset:.12f})/{length:.12f})"
    value = f"({u}-{ramp.strength:.12f}*sin(2*PI*{u})/(2*PI)-{a:.12f})"
    scale = decision.output_frame_count / fps / (b - a)
    return f"{value}*{scale:.12f}/TB"
