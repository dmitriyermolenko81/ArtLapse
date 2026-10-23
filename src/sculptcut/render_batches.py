"""Bounded execution batches without changing the saved edit decisions."""

from collections.abc import Iterable
from dataclasses import dataclass

from sculptcut.domain import SourceMedia
from sculptcut.planning import RenderDecision


@dataclass(frozen=True)
class BatchPolicy:
    maximum_source_us: int = 300_000_000
    maximum_output_seconds: int = 30


DEFAULT_BATCH_POLICY = BatchPolicy()


def coalesce_chunks(
    chunks: Iterable[RenderDecision], sources: dict[str, SourceMedia], fps: int,
    policy: BatchPolicy = DEFAULT_BATCH_POLICY,
) -> list[RenderDecision]:
    """Join contiguous equal-rate, fixed-camera chunks on the source frame grid.

    Keep existing chunk size limits and all stateful/HOLD boundaries. Off-grid
    timestamps and nonzero source origins retain their independent decoder clocks.
    """
    result: list[RenderDecision] = []
    for current in chunks:
        if result and _compatible(result[-1], current, sources[current.source_id], fps, policy):
            previous = result[-1]
            result[-1] = previous.model_copy(update={
                "source_end_us": current.source_end_us,
                "output_frame_count": previous.output_frame_count + current.output_frame_count,
            })
        else:
            result.append(current)
    return result


def _compatible(
    previous: RenderDecision, current: RenderDecision, source: SourceMedia,
    fps: int, policy: BatchPolicy,
) -> bool:
    if (previous.source_id != current.source_id
            or previous.source_end_us != current.source_start_us
            or previous.action not in ("SPEED", "KEEP") or previous.action != current.action
            or previous.subject_camera is not None or current.subject_camera is not None
            or previous.sample_times_us or current.sample_times_us
            or previous.crop != current.crop or previous.provenance != current.provenance
            or previous.transition != current.transition
            or "final" in current.provenance
            or source.start_time != 0 or source.fps_num <= 0):
        return False
    a = previous.source_end_us - previous.source_start_us
    b = current.source_end_us - current.source_start_us
    if (previous.output_frame_count <= 0 or current.output_frame_count <= 0
            or a * current.output_frame_count != b * previous.output_frame_count
            or a + b > policy.maximum_source_us
            or previous.output_frame_count + current.output_frame_count
            > fps * policy.maximum_output_seconds):
        return False
    denominator = 1_000_000 * source.fps_den
    return all(timestamp * source.fps_num % denominator == 0 for timestamp in (
        previous.source_start_us, current.source_start_us, current.source_end_us,
    ))
