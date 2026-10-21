"""Command-line interface for local media processing."""

import argparse
import json
import secrets
from collections.abc import Sequence
from pathlib import Path

from sculptcut import __version__
from sculptcut.analysis import extract_features
from sculptcut.benchmark import benchmark
from sculptcut.cache_management import DAG, cleanup_stale
from sculptcut.domain import Override, Project, ProjectSettings, Roi
from sculptcut.fixtures import generate_suite
from sculptcut.idle import project_idle
from sculptcut.logging_config import configure_logging
from sculptcut.media import MediaError, doctor, probe_dict
from sculptcut.performance import configure_analysis_threads
from sculptcut.planning import RenderPlan, plan_project
from sculptcut.project import create_project, load_project, save_project
from sculptcut.proxy import make_proxies
from sculptcut.quality import evaluate_suite
from sculptcut.rendering import render
from sculptcut.review import review_html
from sculptcut.roi import set_roi
from sculptcut.shorts_layout import SHORTS_PRESETS

COMMANDS = {
    "evaluate": "Compare presets against reviewed annotations",
    "fixtures": "Generate deterministic synthetic test videos",
    "benchmark": "Measure local analysis resources",
    "cleanup-cache": "Remove stale derived cache entries",
    "cache-info": "Show the versioned cache dependency graph",
    "override": "Save a non-destructive review decision",
    "doctor": "Check local media tools and encoders",
    "probe": "Inspect recorded input videos",
    "create-project": "Create a processing project",
    "make-proxy": "Create cached analysis proxies",
    "set-roi": "Set source analysis region",
    "analyze": "Extract cached frame features",
    "plan": "Build an integer-frame timelapse plan",
    "preview": "Render a preview from proxies",
    "render": "Render the final MP4 from originals",
    "run": "Run the processing pipeline",
}


def build_parser() -> argparse.ArgumentParser:
    """Build the public CLI parser."""
    parser = argparse.ArgumentParser(
        prog="sculptcut", description="SculptCut — local timelapse editor."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command, description in COMMANDS.items():
        sub = subparsers.add_parser(command, help=description, description=description)
        if command == "evaluate":
            sub.add_argument("annotations", type=Path)
            sub.add_argument("output", type=Path)
            sub.add_argument("--target-seconds", type=float, default=1.5)
            sub.add_argument("--preset", choices=["timelapse", "shorts"], default="timelapse")
        if command == "fixtures":
            sub.add_argument("directory", type=Path)
        if command in ("benchmark", "cleanup-cache"):
            sub.add_argument("project", type=Path)
        if command == "override":
            sub.add_argument("project", type=Path)
            sub.add_argument("source_id")
            sub.add_argument("start", type=float)
            sub.add_argument("end", type=float)
            sub.add_argument(
                "action",
                choices=["KEEP", "RETAIN", "DROP", "SET_SPEED", "FORCE_MONTAGE", "LOCK_SEGMENT"],
            )
            sub.add_argument("--speed", type=float, default=1)
        if command in ("plan", "render", "preview", "run"):
            sub.add_argument("project", type=Path)
        if command in ("plan", "run"):
            sub.add_argument("--target-seconds", type=float)
            sub.add_argument("--crop-mode", choices=["full", "canvas", "hybrid", "viewport"])
            sub.add_argument("--pacing", choices=["expressive", "restrained"])
            sub.add_argument(
                "--preset",
                choices=[
                    "timelapse", "shorts", "beat_shorts", "music_shorts", "speed",
                    "photoshop_shorts", "photoshop_beat_shorts",
                ],
            )
            sub.add_argument("--sculpt-only", action=argparse.BooleanOptionalAction, default=None)
            sub.add_argument("--final-view-seconds", type=float)
            sub.add_argument("--music", type=Path)
            sub.add_argument("--music-folder", type=Path)
            sub.add_argument("--music-seed", type=int)
            sub.add_argument("--music-selection", choices=["dynamic", "full_tracks"])
            sub.add_argument("--no-music", action="store_true")
            sub.add_argument("--music-mode", choices=["loop", "trim"], default="loop")
            sub.add_argument("--fade-seconds", type=float, default=2)
        if command in ("render", "preview", "run"):
            sub.add_argument("--output", type=Path)
        if command == "run":
            sub.add_argument("sources", type=Path, nargs="*")
        if command == "set-roi":
            sub.add_argument("project", type=Path)
            sub.add_argument("source_id")
            sub.add_argument("coordinates", type=float, nargs=4, metavar="XYWH")
        if command in ("make-proxy", "analyze"):
            sub.add_argument("project", type=Path)
        if command == "create-project":
            sub.add_argument("project", type=Path)
            sub.add_argument("sources", type=Path, nargs="+")
        if command == "probe":
            sub.add_argument("source", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and invoke the shared local services."""
    args = build_parser().parse_args(argv)
    logger = configure_logging()
    configure_analysis_threads()
    try:
        if args.command == "evaluate":
            print(evaluate_suite(args.annotations, args.output, args.target_seconds, args.preset))
            return 0
        if args.command == "fixtures":
            print(generate_suite(args.directory))
            return 0
        if args.command == "cache-info":
            print(json.dumps(DAG, indent=2))
            return 0
        if args.command in ("benchmark", "cleanup-cache"):
            operation = benchmark if args.command == "benchmark" else cleanup_stale
            print(json.dumps(operation(args.project, load_project(args.project)), indent=2))
            return 0
        if args.command == "override":
            project = load_project(args.project)
            override = Override(
                source_id=args.source_id,
                start_us=round(args.start * 1e6),
                end_us=round(args.end * 1e6),
                action=args.action,
                speed=args.speed,
            )
            updated = Project.model_validate(
                project.model_dump() | {"overrides": [*project.overrides, override]}
            )
            save_project(args.project, updated)
            return 0
        if args.command in ("plan", "render", "preview", "run"):
            root = args.project
            project = (
                create_project(root, args.sources)
                if args.command == "run" and args.sources
                else load_project(root)
            )
            if args.command in ("plan", "run"):
                previous_preset = project.settings.preset
                requested_preset = "shorts" if args.sculpt_only else args.preset or previous_preset
                target_seconds = (
                    args.target_seconds
                    if args.target_seconds is not None
                    else 60
                    if requested_preset in SHORTS_PRESETS
                    and previous_preset not in SHORTS_PRESETS
                    else project.settings.target_seconds
                )
                args.target_seconds = target_seconds
            if args.command in ("plan", "run") and (
                args.music or args.music_folder or args.no_music or args.music_selection
            ):
                if args.music and args.music_folder:
                    raise ValueError("Choose a music file or folder, not both")
                music_values: dict[str, object] = {}
                if args.music or args.music_folder or args.no_music:
                    music_values = {
                        "music_path": args.music.resolve()
                        if args.music and not args.no_music
                        else None,
                        "music_folder": args.music_folder.resolve()
                        if args.music_folder and not args.no_music
                        else None,
                        "music_seed": args.music_seed
                        if args.music_seed is not None
                        else secrets.randbits(32),
                        "music_loop": args.music_mode == "loop",
                        "music_fade_seconds": args.fade_seconds,
                    }
                if args.music_selection:
                    music_values["music_selection_mode"] = args.music_selection
                settings = ProjectSettings.model_validate(
                    project.settings.model_dump() | music_values
                )
                project = project.model_copy(update={"settings": settings})
                save_project(root, project)
            if args.command in ("plan", "run") and args.crop_mode:
                settings = ProjectSettings.model_validate(
                    project.settings.model_dump() | {"crop_mode": args.crop_mode}
                )
                project = project.model_copy(update={"settings": settings})
                save_project(root, project)
            if args.command in ("plan", "run") and (
                args.pacing is not None or args.final_view_seconds is not None
            ):
                values = project.settings.model_dump()
                if args.pacing is not None:
                    values["expressive_pacing"] = args.pacing == "expressive"
                if args.final_view_seconds is not None:
                    values["final_view_seconds"] = args.final_view_seconds
                project = project.model_copy(
                    update={"settings": ProjectSettings.model_validate(values)}
                )
                save_project(root, project)
            if args.command in ("plan", "run") and args.preset:
                values = project.settings.model_dump() | {"preset": args.preset}
                values["target_seconds"] = args.target_seconds
                project = project.model_copy(
                    update={"settings": ProjectSettings.model_validate(values)}
                )
                save_project(root, project)
            if args.command in ("plan", "run") and args.sculpt_only is not None:
                strict_values = {"sculpt_only": args.sculpt_only}
                if args.sculpt_only:
                    strict_values.update(
                        {"preset": "shorts", "target_seconds": args.target_seconds}
                    )
                project = project.model_copy(
                    update={"settings": project.settings.model_copy(update=strict_values)}
                )
                save_project(root, project)
            if (
                args.command in ("plan", "run")
                and project.settings.target_seconds != args.target_seconds
            ):
                project = project.model_copy(
                    update={
                        "settings": project.settings.model_copy(
                            update={"target_seconds": args.target_seconds}
                        )
                    }
                )
                save_project(root, project)
            if args.command == "run" and project.settings.preset not in (
                "speed",
                "photoshop_shorts",
                "photoshop_beat_shorts",
            ):
                extract_features(root, project)
            if args.command in ("plan", "run"):
                plan = plan_project(root, project, args.target_seconds)
            else:
                plan = RenderPlan.model_validate_json(
                    (root / "render_plan.json").read_text(encoding="utf-8")
                )
            if args.command != "plan":
                preview = args.command == "preview"
                if preview:
                    make_proxies(root, project)
                output = (
                    args.output or root / ("previews" if preview else "renders") / "timelapse.mp4"
                )
                print(render(root, project, plan, output, preview=preview))
            else:
                print(plan.model_dump_json(indent=2))
            if project.settings.preset not in (
                "speed", "photoshop_shorts", "photoshop_beat_shorts",
            ):
                try:
                    review_html(root, project)
                except (MediaError, OSError, ValueError) as exc:
                    logger.warning(
                        "Edit succeeded; optional review page could not be created: %s", exc,
                        extra={"event": "review_unavailable", "command": args.command},
                    )
            return 0
        if args.command == "analyze":
            extract_features(args.project, load_project(args.project))
            project_idle(args.project, load_project(args.project))
            return 0
        if args.command == "set-roi":
            x, y, w, h = args.coordinates
            roi = Roi(x=x, y=y, width=w, height=h)
            print(set_roi(args.project, load_project(args.project), args.source_id, roi))
            return 0
        if args.command == "make-proxy":
            print(
                json.dumps([str(p) for p in make_proxies(args.project, load_project(args.project))])
            )
            return 0
        if args.command == "create-project":
            result_project = create_project(args.project, args.sources)
            print(result_project.model_dump_json(indent=2))
            return 0
        if args.command == "doctor":
            result = doctor()
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["libx264"] else 1
        if args.command == "probe":
            print(json.dumps(probe_dict(args.source), ensure_ascii=False))
            return 0
    except KeyboardInterrupt:
        logger.warning("Cancelled", extra={"event": "command_cancelled", "command": args.command})
        return 130
    except (MediaError, OSError, ValueError) as exc:
        logger.error(str(exc), extra={"event": "command_failed", "command": args.command})
        return 1
    return 1
