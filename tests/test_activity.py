import numpy as np

from sculptcut.activity import classify
from sculptcut.analysis import measure
from sculptcut.domain import Roi
from sculptcut.motion import Motion


def test_rule_explanations_and_ambiguity() -> None:
    image = np.zeros((64, 64), dtype=np.uint8)
    feature = measure(image, image, Roi(x=0, y=0, width=1, height=1), "a", 0)
    assert classify(feature, Motion()).kind == "STATIC"
    changed = feature.model_copy(update={"roi_change": 0.01})
    assert classify(changed, Motion()).kind == "UNKNOWN"
    sculpt = classify(changed, Motion(quality=0.9, displacement=0, residual=0.01))
    assert sculpt.kind == "SCULPT" and sculpt.components
    camera = classify(changed, Motion(quality=0.9, displacement=5, camera_score=0.9, residual=0.01))
    assert camera.kind == "CAMERA"
    menu = classify(feature.model_copy(update={"ui_change": 0.1}), Motion())
    assert menu.kind == "INTERFACE"
    switch = classify(feature.model_copy(update={"histogram_change": 0.8}), Motion())
    assert switch.kind == "UNKNOWN"


def test_broad_change_and_camera_with_edits_remain_uncertain() -> None:
    image = np.zeros((64, 64), dtype=np.uint8)
    feature = measure(image, image, Roi(x=0, y=0, width=1, height=1), "a", 0)
    feature = feature.model_copy(update={"roi_change": 0.02, "changed_pixel_ratio": 0.4})
    assert classify(feature, Motion(quality=0.9, displacement=0)).kind == "MIXED"
    motion = Motion(quality=0.9, displacement=5, camera_score=0.7, residual=0.02)
    assert classify(feature, motion).kind == "MIXED"


def test_actual_image_local_edit_and_pan() -> None:
    import cv2

    from sculptcut.motion import estimate_motion

    rng = np.random.default_rng(4)
    before = cv2.GaussianBlur(rng.integers(0, 256, (160, 200), dtype=np.uint8), (5, 5), 0)
    local = before.copy()
    local[60:70, 70:80] = 180
    moved = cv2.warpAffine(before, np.float32([[1, 0, 5], [0, 1, 0]]), (200, 160))
    roi = Roi(x=0, y=0, width=1, height=1)
    assert (
        classify(measure(local, before, roi, "a", 0), estimate_motion(before, local)).kind
        == "SCULPT"
    )
    assert (
        classify(measure(moved, before, roi, "a", 0), estimate_motion(before, moved)).kind
        == "CAMERA"
    )
