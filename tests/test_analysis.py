from pathlib import Path
from threading import Event

import numpy as np
import pytest

from sculptcut.analysis import (
    analysis_key,
    brightness_histogram,
    extract_features,
    measure,
    read_features,
)
from sculptcut.domain import ProjectSettings, Roi
from sculptcut.fixtures import generate_fixture
from sculptcut.media import Cancelled, discover, run_process
from sculptcut.project import create_project


def test_fast_histogram_matches_reference_for_all_pixel_values() -> None:
    rng = np.random.default_rng(7)
    for pixels in (np.arange(256, dtype=np.uint8),
                   rng.integers(0, 256, (540, 960), dtype=np.uint8)):
        reference = np.histogram(pixels, bins=32, range=(0, 256))[0].astype(float)
        reference /= reference.sum()
        np.testing.assert_array_equal(brightness_histogram(pixels), reference)


def test_progress_uses_global_time_and_reports_cached_completion(tmp_path: Path) -> None:
    source = generate_fixture(tmp_path / "source.mp4", "static", duration=1)
    root = tmp_path / "project"
    project = create_project(root, [source, source]).model_copy(
        update={"settings": ProjectSettings(proxy_width=128, proxy_fps=5)}
    )
    positions: list[int] = []
    extract_features(root, project, progress=positions.append)
    assert positions == sorted(positions)
    assert max(positions) > project.sources[0].duration_us
    positions.clear()
    extract_features(root, project, progress=positions.append)
    assert positions == [1_000_000, 2_000_000]


def test_feature_measurements() -> None:
    roi = Roi(x=0, y=0, width=0.5, height=1)
    base = np.zeros((100, 100), dtype=np.uint8)
    same = measure(base, base, roi, "a", 0)
    assert same.roi_change == same.ui_change == same.changed_pixel_ratio == 0
    changed = base.copy()
    changed[20:40, 20:40] = 180
    local = measure(changed, base, roi, "a", 200000)
    assert local.roi_change > 0 and local.ui_change == 0
    assert local.edge_change > 0 and local.histogram_change > 0


def test_resume_and_reproducibility(tmp_path: Path) -> None:
    source = tmp_path / "fixture.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=128x96:r=10:d=3",
            "-c:v",
            "libx264",
            str(source),
        ]
    )
    root = tmp_path / "project"
    project = create_project(root, [source]).model_copy(
        update={"settings": ProjectSettings(proxy_width=128, proxy_fps=5)}
    )
    event = Event()

    def cancel_after(timestamp: int) -> None:
        if timestamp >= 800000:
            event.set()

    with pytest.raises(Cancelled):
        extract_features(root, project, event, cancel_after)
    partial = list(read_features(root, project, project.sources[0]))
    assert len(partial) == 5
    extract_features(root, project)
    features = list(read_features(root, project, project.sources[0]))
    assert len(features) == 15
    assert features[:5] == partial
    assert [f.timestamp_us for f in features] == list(range(0, 3000000, 200000))
    extract_features(root, project)
    assert list(read_features(root, project, project.sources[0])) == features
    changed = project.model_copy(
        update={"rois": {project.sources[0].id: Roi(x=0.1, y=0.1, width=0.8, height=0.8)}}
    )
    assert analysis_key(changed, project.sources[0]) != analysis_key(project, project.sources[0])
    assert (root / "logs" / "features.csv").is_file()
