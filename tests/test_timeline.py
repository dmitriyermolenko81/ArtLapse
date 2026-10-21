from pathlib import Path

import pytest

from sculptcut.domain import SourceMedia
from sculptcut.timeline import global_to_source, source_pts, source_to_global, split_global_range


def make_source(index: int, start: int, duration: int) -> SourceMedia:
    return SourceMedia(
        id=str(index),
        path=Path("video"),
        duration_us=duration,
        width=64,
        height=64,
        fps_num=24 + index,
        fps_den=1,
        time_base="1/1000",
        start_time=2_000_000 * index,
        stream_index=0,
        rotation=0,
        audio_streams=[],
        fingerprint="test",
        global_start_us=start,
    )


def test_all_boundaries_and_roundtrip() -> None:
    sources = [make_source(0, 0, 7), make_source(1, 7, 11), make_source(2, 18, 5)]
    for time in range(23):
        source, local = global_to_source(sources, time)
        assert source_to_global(source, local) == time
        assert source_pts(source, local) == source.start_time + local
    assert global_to_source(sources, 7)[0].id == "1"
    for start in range(24):
        for end in range(start, 24):
            parts = split_global_range(sources, start, end)
            assert sum(b - a for _, a, b in parts) == end - start
            assert all(a >= 0 and a < b for _, a, b in parts)
    assert split_global_range(sources, 23, 23) == []
    assert split_global_range(sources, 6, 19) == [("0", 6, 7), ("1", 0, 11), ("2", 0, 1)]
    for time in (-1, 23):
        with pytest.raises(ValueError):
            global_to_source(sources, time)
    with pytest.raises(ValueError):
        split_global_range(sources, 5, 4)
