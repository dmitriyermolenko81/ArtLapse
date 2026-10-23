import numpy as np
import pytest

from sculptcut.image_statistics import MAX_EXACT_HISTOGRAM_PIXELS, median_uint8


@pytest.mark.parametrize("shape", [(1, 1), (1, 2), (3, 5), (16, 16), (270, 480), (540, 960)])
def test_median_matches_numpy_on_even_odd_ties_and_strided_views(shape: tuple[int, int]) -> None:
    rng = np.random.default_rng(205)
    for image in (rng.integers(0, 256, shape, dtype=np.uint8),
                  np.zeros(shape, np.uint8), np.full(shape, 255, np.uint8)):
        for view in (image, image.T, image[::-1, ::-1], image[:, ::2]):
            assert median_uint8(view) == float(np.median(view))
    assert median_uint8(np.array([[0, 255]], dtype=np.uint8)) == 127.5


def test_large_image_avoids_inexact_float32_histogram_counts() -> None:
    image = np.ones((1, MAX_EXACT_HISTOGRAM_PIXELS + 2), dtype=np.uint8)
    assert median_uint8(image) == 1.0
    with pytest.raises(ValueError, match="empty image"):
        median_uint8(np.empty((0, 2), dtype=np.uint8))
