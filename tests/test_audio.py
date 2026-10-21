from pathlib import Path

import numpy as np
import pytest

from sculptcut.audio import add_music, validate_music
from sculptcut.media import MediaError, discover, run_process
from sculptcut.rendering import verify_output


@pytest.mark.parametrize("audio_duration,loop", [(0.3, True), (0.3, False), (3, False)])
def test_music_duration_peak_and_fades(tmp_path: Path, audio_duration: float, loop: bool) -> None:
    video, music, output = (tmp_path / name for name in ("video.mp4", "music.wav", "result.mp4"))
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=s=160x120:r=30:d=1",
            "-c:v",
            "libx264",
            str(video),
        ]
    )
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={audio_duration}",
            str(music),
        ]
    )
    add_music(video, music, output, 1, loop, 0.1)
    verify_output(output, 30, 30, expected_audio=True)
    raw = tmp_path / "audio.f32"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-i",
            str(output),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-f",
            "f32le",
            str(raw),
        ]
    )
    samples = np.fromfile(raw, dtype=np.float32)
    assert float(np.abs(samples).max()) < 1
    assert abs(len(samples) / 48000 - 1) < 0.03
    assert np.abs(samples[:480]).mean() < np.abs(samples[4800:9600]).mean()
    with pytest.raises(MediaError, match="does not exist"):
        validate_music(tmp_path / "missing.wav")
