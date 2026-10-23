"""Bounded, task-local reuse of screen checks on identical image pixels."""

import hashlib
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

DEFAULT_SCREEN_CACHE_ENTRIES = 256
Key = tuple[tuple[int, ...], str, str, bytes]


@dataclass
class _ScreenResults:
    capacity: int
    values: OrderedDict[Key, str | None] = field(default_factory=OrderedDict)


_RESULTS: ContextVar[_ScreenResults | None] = ContextVar("screen_results", default=None)


@contextmanager
def screen_check_cache(max_entries: int = DEFAULT_SCREEN_CACHE_ENTRIES) -> Iterator[None]:
    """Reuse nested checks; discard every result when the owning task finishes."""
    if max_entries < 1:
        raise ValueError("Screen cache capacity must be positive")
    if _RESULTS.get() is not None:
        yield
        return
    token = _RESULTS.set(_ScreenResults(max_entries))
    try:
        yield
    finally:
        _RESULTS.reset(token)


def cached_screen_result(
    image: NDArray[np.uint8], settings_key: Callable[[], str],
    calculate: Callable[[], str | None],
) -> str | None:
    results = _RESULTS.get()
    if results is None:
        return calculate()
    contiguous = np.ascontiguousarray(image)
    key = (image.shape, image.dtype.str, settings_key(),
           hashlib.blake2b(contiguous.data, digest_size=32).digest())
    if key in results.values:
        results.values.move_to_end(key)
        return results.values[key]
    value = calculate()
    results.values[key] = value
    if len(results.values) > results.capacity:
        results.values.popitem(last=False)
    return value
