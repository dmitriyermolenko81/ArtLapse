import gc
import weakref
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import numpy as np
import pytest

from sculptcut.screen_cache import cached_screen_result, screen_check_cache
from sculptcut.screen_cleanup import ScreenSettings, screen_kind


def test_screen_results_follow_pixel_mutations_and_settings() -> None:
    gray = np.full((32, 48), 30, dtype=np.uint8)
    with screen_check_cache():
        assert screen_kind(gray) is None
        assert screen_kind(gray, ScreenSettings(black=40)) == "black_screen"
        gray[:] = 255
        assert screen_kind(gray) == "white_screen"
        gray[:] = 0
        assert screen_kind(gray[:, ::-1]) == "black_screen"


def test_none_is_cached_and_capacity_is_bounded_without_retaining_images() -> None:
    calls = []

    def calculate() -> None:
        calls.append(True)

    gray = np.zeros((4, 4), np.uint8)
    reference = weakref.ref(gray)
    with screen_check_cache(max_entries=2):
        for _ in range(2):
            assert cached_screen_result(gray, lambda: "a", calculate) is None
        assert len(calls) == 1
        cached_screen_result(np.ones_like(gray), lambda: "a", calculate)
        cached_screen_result(np.full_like(gray, 2), lambda: "a", calculate)
        cached_screen_result(gray, lambda: "a", calculate)
        assert len(calls) == 4
        del gray
        gc.collect()
        assert reference() is None
    with pytest.raises(ValueError, match="positive"):
        with screen_check_cache(0):
            pass


def test_task_scope_and_image_shape_are_part_of_reuse_contract() -> None:
    gray = np.zeros((2, 3), np.uint8)
    calls = []

    def calculate() -> str:
        calls.append(True)
        return "measured"

    with screen_check_cache():
        cached_screen_result(gray, lambda: "a", calculate)
        with screen_check_cache():
            cached_screen_result(gray.copy(), lambda: "a", calculate)
        cached_screen_result(gray.reshape(3, 2), lambda: "a", calculate)
        assert len(calls) == 2
    with screen_check_cache():
        cached_screen_result(gray, lambda: "a", calculate)
    assert len(calls) == 3


def test_concurrent_jobs_do_not_share_results() -> None:
    barrier = Barrier(2)

    def job(label: str) -> tuple[str | None, str | None]:
        with screen_check_cache():
            barrier.wait(timeout=10)
            image = np.zeros((2, 2), np.uint8)
            first = cached_screen_result(image, lambda: "same settings", lambda: label)
            second = cached_screen_result(image, lambda: "same settings", lambda: "wrong")
            return first, second

    with ThreadPoolExecutor(max_workers=2) as executor:
        a, b = list(executor.map(job, ("a", "b")))
    assert a == ("a", "a") and b == ("b", "b")


def test_failed_job_discards_its_results() -> None:
    image = np.zeros((2, 2), np.uint8)
    with pytest.raises(RuntimeError, match="cancelled"):
        with screen_check_cache():
            assert cached_screen_result(image, lambda: "key", lambda: "old") == "old"
            raise RuntimeError("cancelled")
    with screen_check_cache():
        assert cached_screen_result(image, lambda: "key", lambda: "new") == "new"
