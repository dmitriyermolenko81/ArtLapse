"""All six Shorts modes audit all work before their native montage/camera pass."""

import json
from pathlib import Path

import pytest
from test_beat_shorts import pulse_track
from test_intro import make_image

from sculptcut.analysis import extract_features
from sculptcut.domain import Override, ProjectSettings, Roi
from sculptcut.fixtures import generate_fixture
from sculptcut.media import probe
from sculptcut.planning import plan_project, validate_plan
from sculptcut.project import create_project, save_project
from sculptcut.rendering import render, verify_output
from sculptcut.shorts_layout import SHORTS_PRESETS


@pytest.mark.parametrize("preset", SHORTS_PRESETS)
@pytest.mark.parametrize("layout", ["auto", "square"])
def test_real_all_short_modes_use_full_catalogue_and_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preset: str, layout: str,
) -> None:
    source = generate_fixture(tmp_path / "working.mp4", "local", duration=72, fps=5)
    music = tmp_path / "music.wav"
    pulse_track(music, [i/2 for i in range(1, 24)], seconds=12)
    cover = tmp_path / "cover.png"
    make_image(cover, (20, 60, 90))
    root = tmp_path / "project"
    project = create_project(root, [source])
    sid = project.sources[0].id
    project = project.model_copy(update={"settings": ProjectSettings(
        preset=preset, target_seconds=12, shorts_layout=layout,
        shorts_square_rois={sid: Roi(x=0.2, y=0.1, width=0.6, height=0.8)},
        proxy_width=384, proxy_fps=5, music_path=music,
        intro_image=cover, intro_seconds=1.5, final_view_seconds=0.5,
    ), "rois": {sid: Roi(x=0.1, y=0.1, width=0.8, height=0.8)},
       "overrides": [Override(source_id=sid, start_us=12_000_000, end_us=16_000_000,
                              action="DROP")]})
    save_project(root, project)
    if not preset.startswith("photoshop"):
        extract_features(root, project)
    plan = plan_project(root, project, 12)
    validate_plan(project, plan)
    assert plan.settings["complete_short_catalogue"] == "short-catalogue-266"
    catalogue = json.loads((root / f"{preset}-work-episodes.json").read_text())["candidates"]
    assert len(catalogue) >= 10 and all(e["process_checked"] for e in catalogue)
    assert plan.settings["short_candidates_verified"] > 0
    assert all(not d.output_frame_count or d.source_end_us <= 12_000_000
               or d.source_start_us >= 16_000_000 for d in plan.decisions)
    assert plan.settings["actual_output_frames"] == 360
    size = (320, 320) if layout == "square" else (320, 568)
    monkeypatch.setattr("sculptcut.rendering.export_size", lambda *args: size)
    output = tmp_path / "short.mp4"
    render(root, project, plan, output, encoder="libx264")
    verify_output(output, 360, 30, expected_audio=True)
    info = probe(output)
    assert (info.width, info.height) == size
