from pathlib import Path

import pytest

from sculptcut.domain import ProjectSettings
from sculptcut.media import discover, run_process
from sculptcut.planning import RenderDecision, build_plan
from sculptcut.project import create_project, fingerprint
from sculptcut.rendering import render, verify_output
from sculptcut.subject_camera import still_frame


@pytest.mark.parametrize("source_fps", [15, 60])
def test_mkv_offset_and_audio_tail_still_exports_and_previews(
    tmp_path: Path, source_fps: int,
) -> None:
    source = tmp_path / "offset-tail.mkv"
    run_process([
        discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
        f"color=c=green:s=160x90:r={source_fps}:d=1.2", "-f", "lavfi", "-i",
        "sine=frequency=440:duration=1.3", "-c:v", "libx264", "-c:a", "pcm_s16le",
        "-output_ts_offset", "0.044", str(source),
    ])
    before = fingerprint(source)
    root = tmp_path / "project"
    project = create_project(root, [source]).model_copy(update={
        "settings": ProjectSettings(preset="photoshop_beat_shorts"),
    })
    media = project.sources[0]
    assert media.start_time > 0
    # Container duration includes a longer audio tail, not real video frames.
    timestamp = media.duration_us - round(1_000_000 / source_fps)
    frame = still_frame(media, timestamp, tmp_path / "still.png")
    assert frame.stat().st_size > 0
    base = build_plan(project, [], 1)
    decisions = [
        RenderDecision(source_id=media.id, source_start_us=0, source_end_us=timestamp,
                       action="DROP", output_frame_count=0, provenance="user:omission"),
        RenderDecision(source_id=media.id, source_start_us=timestamp, source_end_us=timestamp+1,
                       action="HOLD", output_frame_count=30, provenance="user:verified"),
        RenderDecision(source_id=media.id, source_start_us=timestamp+1,
                       source_end_us=media.duration_us, action="DROP", output_frame_count=0,
                       provenance="user:omission"),
    ]
    plan = base.model_copy(update={"decisions": decisions, "actual_frame_count": 30})
    for preview in (False, True):
        output = render(root, project, plan, tmp_path / f"{preview}.mp4",
                        preview=preview, encoder="libx264")
        verify_output(output, 30, 30)
    assert fingerprint(source) == before
