import pytest

from sculptcut.domain import Roi
from sculptcut.roi import masks, pixel_bounds


@pytest.mark.parametrize("width,height", [(1920, 1080), (128, 96), (3840, 2160)])
def test_roi_bounds_masks(width: int, height: int) -> None:
    roi = Roi(x=0.25, y=0.25, width=0.5, height=0.5)
    x, y, right, bottom = pixel_bounds(roi, width, height)
    assert (x, y, right, bottom) == (width // 4, height // 4, width * 3 // 4, height * 3 // 4)
    canvas, ui = masks(roi, width, height)
    assert not (canvas & ui).any()
    assert (canvas | ui).all()


@pytest.mark.parametrize("x,width", [(-0.1, 0.5), (0.8, 0.5), (0, 0), (float("nan"), 0.5)])
def test_invalid_roi(x: float, width: float) -> None:
    with pytest.raises(ValueError):
        Roi(x=x, y=0, width=width, height=1)
