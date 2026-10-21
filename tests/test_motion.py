import cv2
import numpy as np
import pytest

from sculptcut.motion import estimate_motion


@pytest.mark.parametrize("angle,scale,pan", [(0, 1, 5), (0, 1.04, 0), (3, 1, 0)])
def test_pan_zoom_rotation(angle: float, scale: float, pan: int) -> None:
    rng = np.random.default_rng(4)
    original = rng.integers(0, 256, (160, 200), dtype=np.uint8)
    original = cv2.GaussianBlur(original, (5, 5), 0).astype(np.uint8)
    matrix = cv2.getRotationMatrix2D((100, 80), angle, scale)
    matrix[0, 2] += pan
    moved = cv2.warpAffine(original, matrix, (200, 160)).astype(np.uint8)
    result = estimate_motion(original, moved)
    assert result.quality > 0.5
    assert result.camera_score > 0.6
    assert result.residual < 0.08
    assert estimate_motion(original, moved) == result


def test_low_texture_local_and_mixed() -> None:
    blank = np.zeros((160, 200), dtype=np.uint8)
    assert estimate_motion(blank, blank).quality == 0
    rng = np.random.default_rng(4)
    original = rng.integers(0, 256, (160, 200), dtype=np.uint8)
    local = original.copy()
    local[50:100, 50:100] = 255
    assert estimate_motion(original, local).camera_score == 0
    moved = np.roll(original, 5, axis=1)
    moved[20:140, 20:160] = 0
    assert estimate_motion(original, moved).camera_score < 0.6
