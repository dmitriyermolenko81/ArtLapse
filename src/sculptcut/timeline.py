"""Exact half-open timeline mapping independent of nominal frame rate."""

from sculptcut.domain import SourceMedia


def global_to_source(sources: list[SourceMedia], time_us: int) -> tuple[SourceMedia, int]:
    for source in sources:
        local = time_us - source.global_start_us
        if 0 <= local < source.duration_us:
            return source, local
    raise ValueError("Time is outside the half-open timeline")


def source_to_global(source: SourceMedia, local_us: int) -> int:
    if not 0 <= local_us < source.duration_us:
        raise ValueError("Time is outside the source")
    return source.global_start_us + local_us


def source_pts(source: SourceMedia, local_us: int) -> int:
    """Convert relative source microseconds to its presentation timestamp."""
    source_to_global(source, local_us)
    return source.start_time + local_us


def split_global_range(
    sources: list[SourceMedia],
    start_us: int,
    end_us: int,
) -> list[tuple[str, int, int]]:
    total = sum(s.duration_us for s in sources)
    if not 0 <= start_us <= end_us <= total:
        raise ValueError("Invalid global interval")
    result = []
    for source in sources:
        start = max(0, start_us - source.global_start_us)
        end = min(source.duration_us, end_us - source.global_start_us)
        if start < end:
            result.append((source.id, start, end))
    return result
