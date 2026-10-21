from pathlib import Path
from threading import Event

import pytest

from sculptcut.analysis import extract_features
from sculptcut.domain import ProjectSettings
from sculptcut.fixtures import generate_fixture
from sculptcut.jobs import render_job
from sculptcut.media import Cancelled, discover, probe, run_process
from sculptcut.planning import RenderPlan, build_plan, plan_project
from sculptcut.project import create_project
from sculptcut.proxy import make_proxies
from sculptcut.rendering import normalize_timing, render, verify_output
from sculptcut.review import review_html


def test_concatenated_single_frames_keep_exact_clock(tmp_path: Path) -> None:
    fragment = tmp_path / "one.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=s=32x32:r=30",
            "-frames:v",
            "1",
            "-c:v",
            "libx264",
            str(fragment),
        ]
    )
    listing = tmp_path / "concat.txt"
    listing.write_text("file 'one.mp4'\n" * 1000, encoding="utf-8")
    combined = tmp_path / "combined.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "concat",
            "-i",
            str(listing),
            "-c",
            "copy",
            str(combined),
        ]
    )
    fixed = tmp_path / "fixed.mp4"
    normalize_timing(combined, fixed, 30)
    verify_output(fixed, 1000, 30)
    assert abs((probe(fixed).duration_us or 0) - 1000 * 1e6 / 30) < 2


def test_plan_and_review_reuse_and_invalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sculptcut.importance as importance
    import sculptcut.review as review

    source = generate_fixture(tmp_path / "source.mp4", "static", duration=2)
    root = tmp_path / "project"
    project = create_project(root, [source]).model_copy(
        update={"settings": ProjectSettings(proxy_width=128, proxy_fps=5)}
    )
    extract_features(root, project)
    first = plan_project(root, project, 1)
    report = review_html(root, project)
    before = report.stat().st_mtime_ns

    def unexpected(*args: object, **kwargs: object) -> None:
        raise AssertionError("Recomputed unchanged analysis")

    monkeypatch.setattr(importance, "importance_project", unexpected)
    monkeypatch.setattr(review, "review_project", unexpected)
    assert plan_project(root, project, 1) == first
    assert review_html(root, project) == report
    assert report.stat().st_mtime_ns == before
    with pytest.raises(AssertionError, match="Recomputed"):
        plan_project(root, project, 1.5)
    (root / "segments.json").write_text("[]", encoding="utf-8")
    with pytest.raises(AssertionError, match="Recomputed"):
        review_html(root, project)
    event = Event()
    event.set()
    with pytest.raises(Cancelled):
        plan_project(root, project, 1, event)
    plain = first.model_copy(
        update={"settings": {k: v for k, v in first.settings.items() if k != "request_key"}}
    )
    assert render_job(tmp_path / "out.mp4", plain, "libx264", False) == render_job(
        tmp_path / "out.mp4", first, "libx264", False
    )


def test_dense_preview_uses_one_output_encode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sculptcut.rendering as rendering

    source = generate_fixture(tmp_path / "source.mp4", "static", duration=6)
    root = tmp_path / "project"
    project = create_project(root, [source]).model_copy(
        update={"settings": ProjectSettings(proxy_width=128, proxy_fps=5)}
    )
    make_proxies(root, project)
    plan = build_plan(project, [], 100 / 30)
    plan = plan.model_copy(
        update={
            "decisions": [
                plan.decisions[0].model_copy(
                    update={
                        "source_start_us": i * 60000,
                        "source_end_us": (i + 1) * 60000,
                        "output_frame_count": 1,
                    }
                )
                for i in range(100)
            ]
        }
    )
    calls: list[Path] = []

    def tracked(
        source: Path,
        destination: Path,
        fps: int,
        cancel: Event | None = None,
        screen_guard: bool = False,
        bookend_plan: RenderPlan | None = None,
        square_bands: bool = False,
    ) -> list[int]:
        calls.append(source)
        return normalize_timing(
            source, destination, fps, cancel, screen_guard, bookend_plan, square_bands
        )

    monkeypatch.setattr(rendering, "normalize_timing", tracked)
    output = render(root, project, plan, tmp_path / "preview.mp4", preview=True)
    assert len(calls) == 1
    verify_output(output, 100, 30)
