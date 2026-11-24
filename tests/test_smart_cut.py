from pathlib import Path
from threading import Event

from sculptcut.media import discover, probe, run_process
from sculptcut.rendering import verify_output
from sculptcut.smart_cut import splice_video


def test_closed_groups_copy_with_exact_non_keyframe_boundaries(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 "testsrc2=s=160x90:r=30:d=7", "-c:v", "libx264", "-g", "30",
                 "-keyint_min", "30", "-sc_threshold", "0", str(source)])
    output = splice_video(source, tmp_path, [(0, 2_400_000), (3_500_000, 7_000_000)],
                          probe(source), Event())
    assert output
    verify_output(output, 177, 30)
    def hashes(path: Path) -> list[str]:
        data = run_process([discover("ffmpeg"), "-v", "error", "-i", str(path),
                            "-f", "framemd5", "-"])
        return [line.split(",")[-1] for line in data.splitlines() if not line.startswith("#")]
    before, after = hashes(source), hashes(output)
    assert after[:60] == before[:60]
    assert after[-90:] == before[-90:]
