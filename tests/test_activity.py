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


def test_whole_view_stability_does_not_erase_small_local_edits() -> None:
    import cv2

    from sculptcut.motion import estimate_motion

    rng = np.random.default_rng(253)
    before = cv2.GaussianBlur(rng.integers(40, 160, (180, 320), dtype=np.uint8), (5, 5), 0)
    changed = before.copy()
    changed[70:80, 150:162] = 200
    feature = measure(changed, before, Roi(x=0, y=0, width=1, height=1), "a", 0)
    motion = estimate_motion(before, changed)
    assert feature.changed_pixel_ratio > 0.001
    assert classify(feature, motion, perceptually_static=True).kind == "SCULPT"
    # Poor tracking still stays uncertain; a stable global hash is not a reason
    # to throw away a local change whose motion cannot be measured.
    assert classify(feature, Motion(), perceptually_static=True).kind == "UNKNOWN"


def test_quiet_cursor_and_noise_stay_static_under_the_detail_guard() -> None:
    frame = np.full((180, 320), 70, np.uint8)
    still = measure(frame, frame, Roi(x=0, y=0, width=1, height=1), "a", 0)
    cursor = still.model_copy(update={"roi_change":0.00006, "changed_pixel_ratio":0.0005})
    noise = still.model_copy(update={"roi_change":0.0001, "noise":0.01})
    for feature in (still, cursor, noise):
        assert classify(feature, Motion(quality=1), perceptually_static=True).kind == "STATIC"


def test_activity_cache_key_and_cleanup_follow_the_detail_guard(tmp_path, monkeypatch) -> None:
    from dataclasses import replace

    from test_timeline import make_source

    import sculptcut.activity as activity
    from sculptcut.cache_management import cleanup_stale
    from sculptcut.domain import Project
    from sculptcut.project import database

    project = Project(sources=[make_source(0, 0, 10_000_000)])
    current = activity.activity_key(project, project.sources[0])
    connection = database(tmp_path)
    connection.execute("CREATE TABLE activity (source_id TEXT, key TEXT, value TEXT)")
    connection.executemany("INSERT INTO activity VALUES(?,?,?)", [
        ("0", "old-global-hash-key", "old"), ("0", current, "current"),
    ])
    connection.commit()
    connection.close()
    assert cleanup_stale(tmp_path, project)["rows_removed"] == 1
    connection = database(tmp_path)
    assert connection.execute("SELECT value FROM activity").fetchall() == [("current",)]
    connection.close()
    monkeypatch.setattr(activity, "SETTINGS", replace(
        activity.SETTINGS, stable_changed_pixel_fraction=.002,
    ))
    assert activity.activity_key(project, project.sources[0]) != current
