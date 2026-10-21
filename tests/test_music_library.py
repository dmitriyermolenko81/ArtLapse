import json
from pathlib import Path
from threading import Event

import numpy as np
import pytest

from sculptcut.audio import add_music
from sculptcut.domain import ProjectSettings
from sculptcut.media import Cancelled, MediaError, discover, run_process
from sculptcut.music_library import library_files, music_signature, soundtrack
from sculptcut.rendering import verify_output


def test_folder_mix_reuse_duration_and_changes(tmp_path: Path) -> None:
    folder = tmp_path / "Музыка"
    folder.mkdir()
    for index, frequency in enumerate((440, 880, 1320)):
        run_process(
            [
                discover("ffmpeg"),
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={frequency}:duration=0.8",
                str(folder / f"трек {index}.wav"),
            ]
        )
    settings = ProjectSettings(music_folder=folder, music_seed=73, music_fade_seconds=0.2)
    signature = music_signature(settings)
    output = soundtrack(tmp_path, settings, 3)
    assert output is not None
    state = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
    tracks = state["tracks"]
    assert len(tracks) >= 4
    assert len({t["path"] for t in tracks[:3]}) == 3
    assert all(a["path"] != b["path"] for a, b in zip(tracks, tracks[1:], strict=False))
    assert all(t["crossfade_seconds"] > 0 for t in tracks[1:])
    before = output.stat().st_mtime_ns
    assert soundtrack(tmp_path, settings, 3) == output
    assert output.stat().st_mtime_ns == before
    video = tmp_path / "video.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=s=64x64:r=30:d=3",
            "-c:v",
            "libx264",
            str(video),
        ]
    )
    result = tmp_path / "result.mp4"
    add_music(video, output, result, 3, False, 0.2)
    verify_output(result, 90, 30, expected_audio=True)
    raw = tmp_path / "samples.f32"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-i",
            str(result),
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
    assert abs(len(samples) / 48000 - 3) < 0.03
    assert np.abs(samples).max() < 1
    for t in tracks[1:]:
        middle = int((t["start_seconds"] + t["crossfade_seconds"] / 2) * 48000)
        assert np.abs(samples[middle : middle + 480]).mean() > 0.005
    assert music_signature(settings.model_copy(update={"music_seed": 74})) != signature
    event = Event()
    event.set()
    with pytest.raises(Cancelled):
        soundtrack(tmp_path, settings, 3, event)
    (folder / "трек 0.wav").unlink()
    assert music_signature(settings) != signature


def test_empty_and_broken_music_folder(tmp_path: Path) -> None:
    with pytest.raises(MediaError, match="no supported"):
        library_files(tmp_path)
    (tmp_path / "broken.wav").write_bytes(b"not an audio file")
    with pytest.raises(MediaError):
        soundtrack(tmp_path / "project", ProjectSettings(music_folder=tmp_path), 1)
    assert not list((tmp_path / "project" / "music").glob("*.flac"))


def test_full_tracks_start_at_zero_and_crossfade(tmp_path: Path) -> None:
    folder = tmp_path / "music"
    folder.mkdir()
    for index, frequency in enumerate((440, 880, 1320)):
        run_process(
            [
                discover("ffmpeg"),
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={frequency}:duration=1",
                str(folder / f"track-{index}.wav"),
            ]
        )
    settings = ProjectSettings(
        music_folder=folder,
        music_seed=7,
        music_selection_mode="full_tracks",
        music_fade_seconds=0.2,
    )
    assert music_signature(settings) != music_signature(
        settings.model_copy(update={"music_selection_mode": "dynamic"})
    )
    output = soundtrack(tmp_path, settings, 2.2)
    assert output is not None
    state = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
    assert state["selection_mode"] == "full_tracks"
    assert len(state["tracks"]) == 3
    assert all(track["source_start_seconds"] == 0 for track in state["tracks"])
    assert all(track["crossfade_seconds"] > 0 for track in state["tracks"][1:])
