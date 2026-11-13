"""Tiny source remainders and a final zoom must stream with bounded queues."""

from pathlib import Path

import pytest

from sculptcut.bookends import bookend_filter
from sculptcut.fixtures import generate_fixture
from sculptcut.planning import build_plan, validate_plan
from sculptcut.project import create_project
from sculptcut.rendering import render, verify_output


@pytest.mark.parametrize("action", ["SPEED", "KEEP"])
def test_real_one_microsecond_remainder_exports_one_frame(tmp_path: Path, action: str) -> None:
    source = generate_fixture(tmp_path / "local.mp4", "local", duration=2, fps=30)
    root = tmp_path / "project"
    project = create_project(root, [source])
    base = build_plan(project, [], 1)
    d = base.decisions[0]
    plan = base.model_copy(update={"decisions": [
        d.model_copy(update={"source_end_us": 1_000_000, "output_frame_count": 29}),
        d.model_copy(update={"source_start_us": 1_000_000, "source_end_us": 1_000_001,
                             "output_frame_count": 1, "action": action}),
        d.model_copy(update={"source_start_us": 1_000_001, "action": "DROP",
                             "output_frame_count": 0}),
    ]})
    validate_plan(project, plan)
    output = tmp_path / "result.mp4"
    render(root, project, plan, output, encoder="libx264")
    verify_output(output, 30, 30)


def test_final_zoom_has_no_branches_which_buffer_the_movie() -> None:
    graph = bookend_filter(108_000, 120, 30, 2560, 1440)
    assert "split=" not in graph and "concat=" not in graph
    assert "107880" in graph and "perspective" in graph
