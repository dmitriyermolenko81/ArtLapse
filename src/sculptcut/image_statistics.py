"""Exact statistics for bounded uint8 analysis images."""

import cv2
import numpy as np
from numpy.typing import NDArray

# OpenCV histogram bins use float32; integer counts are exact below this limit.
MAX_EXACT_HISTOGRAM_PIXELS = 2**24 - 1


def median_uint8(image: NDArray[np.uint8]) -> float:
    """Return the same even/odd median as NumPy without partitioning the pixels."""
    if not image.size:
        raise ValueError("Cannot measure an empty image")
    if image.ndim != 2 or image.size > MAX_EXACT_HISTOGRAM_PIXELS:
        return float(np.median(image))
    counts = cv2.calcHist([image], [0], None, [256], [0, 256])
    cumulative = counts.ravel().astype(np.int64).cumsum()
    low, high = np.searchsorted(cumulative, [(image.size - 1) // 2 + 1, image.size // 2 + 1])
    return (int(low) + int(high)) / 2
