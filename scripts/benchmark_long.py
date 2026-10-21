"""Measure the real SculptCut pipeline on one long source without modifying it."""

import argparse
import json
import platform
import subprocess
from collections.abc import Callable
from pathlib import Path
from time import perf_counter
from typing import TypeVar

from sculptcut.analysis import extract_features
from sculptcut.domain import Project, ProjectSettings
from sculptcut.planning import RenderPlan, plan_project
from sculptcut.project import create_project, save_project
from sculptcut.proxy import make_proxies
from sculptcut.rendering import render, verify_output

T = TypeVar("T")


def directory_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    source = args.source.resolve()
    root = args.root.resolve()
    if root.exists() and any(root.iterdir()):
        raise RuntimeError("Benchmark directory must be new or empty")

    report: dict[str, object] = {
        "source": str(source),
        "source_bytes": source.stat().st_size,
        "system": platform.platform(),
        "stages": {},
    }

    def save() -> None:
        root.mkdir(parents=True, exist_ok=True)
        (root / "benchmark.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def measure(name: str, operation: Callable[[], T]) -> T:
        print(f"START {name}", flush=True)
        started = perf_counter()
        result = operation()
        elapsed = perf_counter() - started
        stages = report["stages"]
        assert isinstance(stages, dict)
        stages[name] = {"seconds": round(elapsed, 3), "project_bytes": directory_bytes(root)}
        save()
        print(f"DONE {name}: {elapsed:.1f}s", flush=True)
        return result

    try:
        gpu = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        report["gpu"] = gpu.stdout.strip() or "unavailable"
    except (OSError, subprocess.TimeoutExpired):
        report["gpu"] = "unavailable"

    project = measure("metadata", lambda: create_project(root, [source]))
    settings = ProjectSettings.model_validate(
        project.settings.model_dump()
        | {
            "target_seconds": 60,
            "preset": "shorts",
            "sculpt_only": True,
            "music_path": None,
            "music_folder": None,
        }
    )
    project = Project.model_validate(project.model_dump() | {"settings": settings})
    save_project(root, project)
    measure("proxy", lambda: make_proxies(root, project))
    measure("analysis", lambda: extract_features(root, project))
    plan = measure("planning", lambda: plan_project(root, project, 60))
    assert isinstance(plan, RenderPlan)
    encoders: list[str] = []
    destination = root / "renders" / "benchmark.mp4"
    measure(
        "render",
        lambda: render(root, project, plan, destination, encoder_status=encoders.append),
    )
    verify_output(destination, plan.actual_frame_count, plan.output_fps)
    report["encoder"] = encoders[-1] if encoders else "unknown"
    report["output_bytes"] = destination.stat().st_size
    report["output_seconds"] = plan.actual_frame_count / plan.output_fps
    save()


if __name__ == "__main__":
    main()
