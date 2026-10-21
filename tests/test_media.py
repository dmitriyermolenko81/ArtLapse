"""Real FFmpeg checks: these fail if the required media tools are unavailable."""

import json
import sys
from pathlib import Path
from threading import Event, Timer

import pytest

from sculptcut.media import Cancelled, MediaError, discover, parse_probe, probe, run_process


def test_real_unicode_video_and_invalid_sources(tmp_path: Path) -> None:
    source = tmp_path / "видео с пробелом.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=128x96:r=10:d=1",
            "-c:v",
            "libx264",
            str(source),
        ]
    )
    info = probe(source)
    assert info.duration_us == 1_000_000
    assert (info.width, info.height, info.fps_num, info.fps_den) == (128, 96, 10, 1)
    assert info.audio_streams == []
    with pytest.raises(MediaError, match="does not exist"):
        probe(tmp_path / "missing.mp4")
    source.write_bytes(b"invalid")
    with pytest.raises(MediaError, match="Process exited"):
        probe(source)


def test_probe_missing_fields_and_nonzero_start() -> None:
    info = parse_probe(
        json.dumps(
            {
                "streams": [
                    {
                        "index": 3,
                        "codec_type": "video",
                        "start_time": "2.5",
                        "avg_frame_rate": "0/0",
                        "side_data_list": [{"rotation": 90}],
                    }
                ]
            }
        ),
        Path("video"),
    )
    assert info.duration_us is None
    assert info.start_time == 2_500_000
    assert info.stream_index == 3
    assert info.rotation == 90
    with pytest.raises(MediaError, match="No video"):
        parse_probe("{}", Path("video"))


def test_process_failure_cancellation_and_progress() -> None:
    with pytest.raises(MediaError, match="exited 7"):
        run_process([sys.executable, "-c", "import sys; sys.exit(7)"])
    event = Event()
    timer = Timer(0.2, event.set)
    timer.start()
    try:
        with pytest.raises(Cancelled):
            run_process([sys.executable, "-c", "import time; time.sleep(30)"], event)
    finally:
        timer.cancel()
    chunks: list[str] = []
    run_process([sys.executable, "-c", "print('progress=end')"], progress=chunks.append)
    assert "progress=end" in "".join(chunks)


@pytest.mark.parametrize("tag, expected", [
    ("00:00:05.500000000", 3_000_000), ("invalid", 8_000_000),
    ("00:00:00.000000000", 8_000_000),
])
def test_matroska_video_duration_before_audio_tail(tag: str, expected: int) -> None:
    info = parse_probe(json.dumps({
        "streams": [{"index": 0, "codec_type": "video", "start_time": "2.5",
                     "tags": {"DURATION": tag}}],
        "format": {"duration": "8"},
    }), Path("video.webm"))
    assert info.duration_us == expected
