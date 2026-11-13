"""Static UI cannot hide model navigation from the work-only selector."""

from pathlib import Path

import cv2
import numpy as np
import pytest
from test_timelapse_cleanup import model_pair

from sculptcut.domain import ProjectSettings, Roi
from sculptcut.project import create_project
from sculptcut.proxy import make_proxies
from sculptcut.shorts_quality import EpisodeInspector


@pytest.mark.parametrize("kind", ["zoom", "static", "work"])
def test_real_subject_process_gate_with_static_interface(tmp_path: Path, kind: str) -> None:
    before, _ = model_pair()
    video = tmp_path / "subject.mp4"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 10, (512, 320))
    assert writer.isOpened()
    for i in range(30):
        image = before.copy()
        if kind == "zoom":
            matrix = cv2.getRotationMatrix2D((250, 200), 0, 1 + i / 29 * 0.18)
            image = cv2.warpAffine(image, matrix, (512, 320), borderValue=70)
        if kind == "work" and i >= 10:
            image[180:200, 240:260] = 225
        image[:30] = 35
        image[:, :65] = 45
        image[:, -45:] = 45
        cv2.circle(image, (440, 60), 12, 180, -1)
        writer.write(cv2.cvtColor(np.uint8(image), cv2.COLOR_GRAY2BGR))
    writer.release()
    root = tmp_path / "project"
    project = create_project(root, [video])
    project = project.model_copy(update={"settings": ProjectSettings(proxy_width=512, proxy_fps=10),
        "rois": {project.sources[0].id: Roi(x=0.13, y=0.1, width=0.78, height=0.88)}})
    make_proxies(root, project)
    with EpisodeInspector(root, project, work_only=True) as inspector:
        score = inspector.development(project.sources[0].id, 0, 3_000_000).score
    assert (score is not None and score >= 0.04) == (kind == "work")
