import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from threading import Event

import cv2
import pytest

from sculptcut.analysis import FrameFeature, Gray, extract_features, read_features
from sculptcut.benchmark import benchmark
from sculptcut.cache_management import cleanup_stale
from sculptcut.domain import ProjectSettings, Roi
from sculptcut.media import Cancelled, discover, run_process
from sculptcut.planning import build_plan
from sculptcut.project import create_project, database
from sculptcut.rendering import render, verify_output


def test_render_resume_reuses_verified_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sculptcut.rendering as rendering

    source = tmp_path / "source.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=160x120:r=15:d=2",
            "-c:v",
            "libx264",
            str(source),
        ]
    )
    root = tmp_path / "project"
    project = create_project(root, [source, source])
    plan = build_plan(project, [], 1)
    destination = tmp_path / "resume.mp4"
    event = Event()

    def interrupt(value: float) -> None:
        event.set()

    with pytest.raises(Cancelled):
        render(root, project, plan, destination, event, encoder="libx264", progress=interrupt)
    assert not destination.exists()
    chunks = list((tmp_path / ".sculptcut-jobs").rglob("chunk-*.mp4.json"))
    assert len(chunks) == 1
    launches: list[int] = []

    def count_launches(
        args: Sequence[str],
        cancel: Event | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> str:
        if "-frames:v" in args:
            launches.append(1)
        return run_process(args, cancel, progress)

    monkeypatch.setattr(rendering, "run_process", count_launches)
    render(root, project, plan, destination, encoder="libx264")
    assert len(launches) == 1
    verify_output(destination, 30, 30)


def test_completed_analysis_avoids_decode_and_reports_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    source = tmp_path / "source.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=128x96:r=10:d=2",
            "-c:v",
            "libx264",
            str(source),
        ]
    )
    root = tmp_path / "project"
    project = create_project(root, [source]).model_copy(
        update={"settings": ProjectSettings(proxy_width=128)}
    )
    extract_features(root, project)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Completed analysis must not decode video")

    monkeypatch.setattr(cv2, "VideoCapture", forbidden)
    result = benchmark(root, project)
    assert result["project_disk_bytes"]
    assert result["process_peak_working_set_bytes"]
    connection = database(root)
    with connection:
        connection.execute(
            "INSERT INTO features VALUES(?,?,?,?)", ("source-0000", "stale", 0, "{}")
        )
    connection.close()
    stale = root / "proxy" / "source-0000-stale.mp4"
    stale.write_bytes(b"derived")
    private = root / "proxy" / "notes.txt"
    private.write_text("keep me")
    assert cleanup_stale(root, project) == {"rows_removed": 1, "proxy_files_removed": 1}
    assert private.read_text() == "keep me" and source.is_file()


def test_analysis_failure_retains_committed_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sculptcut.analysis as analysis

    source = tmp_path / "source.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=128x96:r=5:d=25",
            "-c:v",
            "libx264",
            str(source),
        ]
    )
    root = tmp_path / "project"
    project = create_project(root, [source]).model_copy(
        update={"settings": ProjectSettings(proxy_width=128)}
    )
    real_measure = analysis.measure
    calls = 0

    def crash(
        current: Gray,
        previous: Gray,
        roi: Roi,
        source_id: str,
        timestamp: int,
        edge_pair: tuple[Gray, Gray] | None = None,
        *precomputed: object,
    ) -> FrameFeature:
        nonlocal calls
        calls += 1
        if calls == 105:
            raise RuntimeError("injected analysis crash")
        return real_measure(current, previous, roi, source_id, timestamp, edge_pair, *precomputed)

    monkeypatch.setattr(analysis, "measure", crash)
    with pytest.raises(RuntimeError, match="crash"):
        extract_features(root, project)
    assert len(list(read_features(root, project, project.sources[0]))) == 100
    monkeypatch.setattr(analysis, "measure", real_measure)
    extract_features(root, project)
    assert len(list(read_features(root, project, project.sources[0]))) == 125


def test_longer_input_does_not_scale_analysis_memory(tmp_path: Path) -> None:
    peaks = []
    for duration in (2, 20):
        source = tmp_path / f"source-{duration}.mp4"
        run_process(
            [
                discover("ffmpeg"),
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=s=128x96:r=10:d={duration}",
                "-c:v",
                "libx264",
                str(source),
            ]
        )
        root = tmp_path / f"project-{duration}"
        project = create_project(root, [source]).model_copy(
            update={"settings": ProjectSettings(proxy_width=128)}
        )
        from sculptcut.project import save_project

        save_project(root, project)
        result = subprocess.run(
            [sys.executable, "-m", "sculptcut", "benchmark", str(root)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        peaks.append(int(json.loads(result.stdout)["process_peak_working_set_bytes"]))
    assert peaks[1] < peaks[0] * 2.5
