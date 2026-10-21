import numpy as np

from sculptcut.analysis import measure
from sculptcut.domain import Roi
from sculptcut.review import visual_returns


def test_visual_return_is_only_review() -> None:
    frame = np.random.default_rng(42).integers(0, 255, (64, 64), dtype=np.uint8)
    base = measure(frame, frame, Roi(x=0, y=0, width=1, height=1), "a", 0)
    values = ["0", "0", "ffffffff", "ffffffff", "0", "0"]
    features = [
        base.model_copy(update={"timestamp_us": i * 1_000_000, "perceptual_hash": value})
        for i, value in enumerate(values)
    ]
    result = visual_returns(features)
    assert result and all(r.action == "REVIEW" for r in result)
    progressed = [
        f.model_copy(update={"perceptual_hash": "ffff0000"}) if i >= 4 else f
        for i, f in enumerate(features)
    ]
    assert visual_returns(progressed) == []
    switched = [
        f.model_copy(update={"histogram_change": 0.9}) if i == 2 else f
        for i, f in enumerate(features)
    ]
    assert visual_returns(switched) == []
