import numpy as np

from sculptcut.analysis import measure
from sculptcut.domain import Roi
from sculptcut.montage import stable_states


def test_stable_states_extremes_and_fallback() -> None:
    frame = np.random.default_rng(42).integers(0, 255, (64, 64), dtype=np.uint8)
    base = measure(frame, frame, Roi(x=0, y=0, width=1, height=1), "a", 0)
    features = [
        base.model_copy(
            update={"timestamp_us": i * 200000, "perceptual_hash": hex(i * 1234567)[2:]}
        )
        for i in range(50)
    ]
    selected = stable_states(features, 0, 10_000_000)
    assert selected[0] == 0 and selected[-1] >= 9_000_000
    assert len(selected) <= 12 and selected == sorted(set(selected))
    assert (
        stable_states([f.model_copy(update={"roi_change": 0.1}) for f in features], 0, 10_000_000)
        == []
    )
