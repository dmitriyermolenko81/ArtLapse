import numpy as np

from sculptcut.analysis import Gray, measure
from sculptcut.domain import Roi
from sculptcut.idle import IdleSettings, detect_idle


def test_static_short_pause_and_boundary_padding() -> None:
    frame = np.full((100, 100), 80, dtype=np.uint8)
    roi = Roi(x=0, y=0, width=1, height=1)
    features = [measure(frame, frame, roi, "a", i * 200000) for i in range(40)]
    ranges = detect_idle(features, 8_000_000, 200000)
    assert [(r.start_us, r.end_us) for r in ranges] == [(700000, 7500000)]
    assert detect_idle(features[:10], 2_000_000, 200000) == []
    features[20] = features[20].model_copy(update={"roi_change": 0.1, "signed_change": 0.1})
    assert detect_idle(features, 8_000_000, 200000) == []


def test_cursor_noise_and_slow_local_change() -> None:
    roi = Roi(x=0, y=0, width=1, height=1)
    base = np.full((200, 200), 100, dtype=np.uint8)
    settings = IdleSettings(minimum_us=1_000_000, padding_us=0)
    for mode in ("cursor", "noise", "slow"):
        rng = np.random.default_rng(42)
        previous: Gray = base.copy()
        features = []
        for index in range(30):
            frame: Gray = base.copy()
            if mode == "cursor":
                frame[50:52, index * 3 : index * 3 + 2] = 240
            elif mode == "noise":
                frame = (base.astype(np.int16) + rng.integers(-1, 2, base.shape)).astype(np.uint8)
            else:
                frame[50:90, 50:90] += index
            features.append(measure(frame, previous, roi, "a", index * 200000))
            previous = frame
        ranges = detect_idle(features, 6_000_000, 200000, settings)
        if mode == "slow":
            assert ranges == []
        else:
            assert sum(r.end_us - r.start_us for r in ranges) >= 4_000_000
