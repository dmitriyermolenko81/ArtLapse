from pathlib import Path
from threading import Event

import pytest

from sculptcut.domain import ProjectSettings, Roi
from sculptcut.media import Cancelled, discover, probe, run_process
from sculptcut.project import create_project, fingerprint, load_project
from sculptcut.proxy import make_proxies
from sculptcut.roi import set_roi


@pytest.mark.parametrize("vfr", [False, True])
@pytest.mark.parametrize("offset", [0, 3])
def test_proxy_timing_cache_and_cancel(tmp_path: Path, vfr: bool, offset: int) -> None:
    source = tmp_path / "original.mp4"
    args = [discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=128x96:r=20:d=2"]
    if vfr:
        args += ["-vf", "select='if(lt(t,1),1,not(mod(n,2)))'", "-fps_mode", "vfr"]
    run_process([*args, "-c:v", "libx264", "-output_ts_offset", str(offset), str(source)])
    before = fingerprint(source)
    root = tmp_path / "project"
    project = create_project(root, [source]).model_copy(
        update={"settings": ProjectSettings(proxy_width=128, proxy_fps=5)}
    )
    updates: list[tuple[str, float]] = []
    output = make_proxies(
        root, project, progress=lambda stage, value: updates.append((stage, value))
    )[0]
    values = [
        value
        for stage, value in updates
        if stage.startswith("Creating and checking analysis proxy:")
    ]
    assert values[0] == 0 and values[-1] == 1
    # Very short fixtures can finish between two process polling intervals.
    assert values == sorted(values)
    info = probe(output)
    assert info.duration_us is not None
    assert abs(info.duration_us - project.sources[0].duration_us) <= 400000
    assert (info.fps_num, info.fps_den, info.audio_streams) == (5, 1, [])
    mtime = output.stat().st_mtime_ns
    assert make_proxies(root, project) == [output]
    assert output.stat().st_mtime_ns == mtime
    event = Event()
    event.set()
    other = project.model_copy(update={"settings": ProjectSettings(proxy_width=160)})
    with pytest.raises(Cancelled):
        make_proxies(root, other, event)
    assert not list((root / "temp").iterdir())
    overlay = set_roi(
        root, project, project.sources[0].id, Roi(x=0.1, y=0.1, width=0.8, height=0.8)
    )
    assert overlay.read_bytes().startswith(b"\x89PNG")
    assert project.sources[0].id in load_project(root).rois
    assert output.stat().st_mtime_ns == mtime
    assert fingerprint(source) == before


def test_resolution_change_preserves_entire_proxy_timeline(tmp_path: Path) -> None:
    parts = []
    for index, size in enumerate(("128x96", "160x120")):
        part = tmp_path / f"part-{index}.mp4"
        run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                     f"testsrc2=s={size}:r=20:d=2", "-c:v", "libx264", str(part)])
        parts.append(part)
    listing = tmp_path / "parts.txt"
    listing.write_text("".join(f"file '{part.name}'\n" for part in parts), encoding="utf-8")
    source = tmp_path / "changing-resolution.mp4"
    run_process([discover("ffmpeg"), "-v", "error", "-f", "concat", "-safe", "0",
                 "-i", str(listing), "-c", "copy", str(source)])
    original = fingerprint(source)
    root = tmp_path / "project"
    project = create_project(root, [source]).model_copy(
        update={"settings": ProjectSettings(proxy_width=128, proxy_fps=5)}
    )
    output = make_proxies(root, project)[0]
    assert probe(output).duration_us == 4_000_000
    from sculptcut.rendering import verify_output

    verify_output(output, 20, 5)
    from sculptcut.planning import simple_speed_plan
    from sculptcut.rendering import render

    plan = simple_speed_plan(project, 2)
    exported = render(root, project, plan, root / "renders" / "result.mp4", encoder="libx264")
    verify_output(exported, plan.actual_frame_count, plan.output_fps)
    assert fingerprint(source) == original
