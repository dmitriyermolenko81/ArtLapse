"""Repeatable local validation with isolated smoke data and persistent evidence."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Validation:
    def __init__(self, workspace: Path) -> None:
        # Refuse even an empty existing directory: never overwrite earlier evidence.
        workspace.mkdir(parents=True, exist_ok=False)
        self.workspace = workspace
        self.environment = os.environ.copy()
        self.environment.update({
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONIOENCODING": "utf-8",
            "SCULPTCUT_DATA_DIR": str(workspace / "data"),
        })
        self.report: dict[str, object] = {
            "status": "RUNNING", "python": sys.executable,
            "started_utc": datetime.now(timezone.utc).isoformat(), "steps": [],
        }
        self.save()

    def save(self) -> None:
        temporary = self.workspace / "report.tmp"
        temporary.write_text(
            json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        temporary.replace(self.workspace / "report.json")

    def run(self, name: str, command: list[str], timeout: int = 180) -> str:
        steps = self.report["steps"]
        assert isinstance(steps, list)
        step: dict[str, object] = {"name": name, "command": command, "status": "RUNNING"}
        steps.append(step)
        self.save()
        print(f"START {name}", flush=True)
        started = perf_counter()
        try:
            with (self.workspace / f"{name}.log").open("wb") as log:
                result = subprocess.run(
                    command, cwd=ROOT, env=self.environment,
                    stdout=log, stderr=subprocess.STDOUT, timeout=timeout, check=False,
                )
            step["returncode"] = result.returncode
            if result.returncode:
                raise RuntimeError(f"{name} failed; see {self.workspace / (name + '.log')}")
            step["status"] = "PASS"
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            step["status"] = "FAIL"
            step["error"] = str(error)
            self.report["status"] = "FAIL"
            raise
        except KeyboardInterrupt:
            step["status"] = "INTERRUPTED"
            self.report["status"] = "INTERRUPTED"
            raise
        finally:
            step["seconds"] = round(perf_counter() - started, 3)
            self.save()
        print(f"PASS {name} ({step['seconds']}s)", flush=True)
        return (self.workspace / f"{name}.log").read_text(encoding="utf-8", errors="replace")

    def smoke(self) -> None:
        python = sys.executable
        self.run("doctor", [python, "-m", "sculptcut", "doctor"])
        # Resolve configured tools using the same discovery rules as the application.
        tools = json.loads(self.run("tools", [python, "-c", (
            "import json; from sculptcut.media import discover; "
            "print(json.dumps([discover('ffmpeg'), discover('ffprobe')]))"
        )]))
        source = self.workspace / "исходник с пробелом.mp4"
        output = self.workspace / "результат.mp4"
        self.run("fixture", [
            tools[0], "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x120:r=15:d=2",
            "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", str(source),
        ])
        original_hash = file_hash(source)
        self.run("pipeline", [
            python, "-m", "sculptcut", "run", str(self.workspace / "проект"), str(source),
            "--target-seconds", "1", "--output", str(output),
        ])
        metadata = json.loads(self.run("ffprobe", [
            tools[1], "-v", "error", "-count_frames", "-show_streams", "-of", "json", str(output),
        ]))
        videos = [stream for stream in metadata["streams"] if stream["codec_type"] == "video"]
        if (len(videos) != 1 or int(videos[0]["nb_read_frames"]) != 30
                or videos[0]["avg_frame_rate"] != "30/1"
                or any(stream["codec_type"] == "audio" for stream in metadata["streams"])):
            raise RuntimeError("Smoke export must contain 30 frames at 30 fps without audio")
        if file_hash(source) != original_hash:
            raise RuntimeError("Smoke pipeline changed the original source")
        self.run("decode", [
            tools[0], "-v", "error", "-xerror", "-i", str(output), "-f", "null", "-",
        ])
        gui = self.workspace / "gui"
        self.run("gui", [
            python, "-c", "from sculptcut.desktop import main; raise SystemExit(main())",
            "--smoke-directory", str(gui), "--smoke-video", str(output),
        ], timeout=60)
        window = json.loads((gui / "window.json").read_text(encoding="utf-8"))
        if not (window["visible"] and window["screenshot"] and window["video_frames"] > 0):
            raise RuntimeError("GUI did not display and decode the smoke export")
        self.report["smoke"] = {
            "frames": 30, "fps": "30/1", "source_unchanged": True, "window": window,
        }
        self.save()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, help="New directory for logs and synthetic media")
    args = parser.parse_args()
    workspace = args.workspace or ROOT / "artifacts" / f"validation-{uuid4().hex[:12]}"
    validation = Validation(workspace.resolve())
    try:
        for name, arguments in (
            ("pytest", ["pytest", "-q"]),
            ("ruff", ["ruff", "check", "."]),
            ("mypy", ["mypy", "src"]),
        ):
            validation.run(name, [sys.executable, "-m", *arguments], timeout=3600)
        validation.smoke()
        validation.report["status"] = "PASS"
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as error:
        validation.report.update(status="FAIL", error=str(error))
        print(str(error), file=sys.stderr)
    except KeyboardInterrupt:
        validation.report["status"] = "INTERRUPTED"
    finally:
        validation.save()
        print(f"Report: {validation.workspace / 'report.json'}", flush=True)
    return 0 if validation.report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
