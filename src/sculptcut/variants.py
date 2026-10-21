"""Per-export snapshots; never overwrite the history when rebuilding a plan."""

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sculptcut.domain import Project
from sculptcut.export_storage import metadata_directory, metadata_path, music_relative_path
from sculptcut.planning import RenderPlan
from sculptcut.project import atomic_write


def read_variant(video: Path) -> dict[str, Any]:
    path = metadata_path(video, ".variant.json")
    if not path.is_file():
        path = video.with_suffix(".variant.json")
    if not path.is_file():
        return {}
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("Неизвестная версия истории ролика")
    for field in ("project", "plan", "music"):
        if data.get(field) is not None and not isinstance(data[field], dict):
            raise ValueError(f"Повреждённая история ролика: {field}")
    if data.get("parent") is not None and not isinstance(data["parent"], str):
        raise ValueError("Повреждённая история ролика: parent")
    cuts = data.get("cuts_us", [])
    if not isinstance(cuts, list) or any(
        not isinstance(cut, list) or len(cut) != 2
        or not all(isinstance(value, int) for value in cut)
        or not 0 <= cut[0] < cut[1] for cut in cuts
    ):
        raise ValueError("Повреждённая история ролика: cuts_us")
    if data.get("operation") == "cut" and "cuts_us" not in data:
        raise ValueError("В истории подрезки отсутствуют вырезки")
    music = data.get("music")
    if isinstance(music, dict) and isinstance(music.get("path"), str):
        relative = Path(music["path"])
        if relative.parts and relative.parts[0] == ".variant-assets":
            relocated = metadata_directory(video) / relative
            if not (video.parent / relative).is_file() and relocated.is_file():
                music["path"] = music_relative_path(relocated, video)
    return data


def cut_history(video: Path) -> list[list[tuple[int, int]]]:
    """Return cuts in application order, including legacy parent snapshots."""
    chain: list[list[tuple[int, int]]] = []
    visited: set[Path] = set()
    current = video.resolve()
    while True:
        if current in visited:
            raise ValueError("В истории ролика обнаружен цикл")
        visited.add(current)
        state = read_variant(current)
        if "cut_history_us" in state:
            raw = state["cut_history_us"]
            if not isinstance(raw, list) or any(
                not isinstance(batch, list) or any(
                    not isinstance(cut, list) or len(cut) != 2
                    or not all(isinstance(value, int) for value in cut)
                    or not 0 <= cut[0] < cut[1] for cut in batch
                ) for batch in raw
            ):
                raise ValueError("Повреждённая история ролика: cut_history_us")
            return [[(a, b) for a, b in batch] for batch in raw] + list(reversed(chain))
        if state.get("operation") == "shorts_refill":
            return list(reversed(chain))
        if state.get("cuts_us"):
            chain.append([(a, b) for a, b in state["cuts_us"]])
        parent = state.get("parent")
        if not parent or state.get("operation") in ("render", "shorts_refill"):
            return list(reversed(chain))
        candidate = (current.parent / parent).resolve()
        if candidate.parent != current.parent:
            raise ValueError("Недопустимый путь в истории ролика")
        if not (metadata_path(candidate, ".variant.json").is_file()
                or candidate.with_suffix(".variant.json").is_file()):
            raise ValueError("Для восстановления старых вырезок нужна история предыдущей версии")
        current = candidate


def save_variant(
    video: Path,
    *,
    project: Project | None = None,
    plan: RenderPlan | None = None,
    music: Path | None = None,
    parent: Path | None = None,
    cuts: list[tuple[int, int]] | None = None,
    operation: str = "render",
    history: list[list[tuple[int, int]]] | None = None,
) -> None:
    snapshot = metadata_path(video, ".variant.json")
    if snapshot.exists() or video.with_suffix(".variant.json").exists():
        raise ValueError("История этого варианта уже существует")
    inherited = read_variant(parent) if parent else {}
    soundtrack = inherited.get("music")
    if music:
        assets = metadata_directory(video) / ".variant-assets" / video.stem
        assets.mkdir(parents=True, exist_ok=True)
        saved_music = assets / ("soundtrack" + music.suffix)
        shutil.copy2(music, saved_music)
        manifest = music.with_suffix(".json")
        soundtrack = {
            "path": music_relative_path(saved_music, video),
            "tracks": json.loads(manifest.read_text(encoding="utf-8"))
            if manifest.is_file()
            else None,
        }
    if history is None:
        history = cut_history(parent) if parent else []
        if operation == "shorts_refill":
            history = []  # This plan already incorporates cuts and refills its duration.
        elif cuts:
            history.append(cuts)
    state = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "video": video.name,
        "operation": operation,
        "parent": parent.name if parent else None,
        "project": project.model_dump(mode="json") if project else inherited.get("project"),
        "plan": plan.model_dump(mode="json") if plan else inherited.get("plan"),
        "music": soundtrack,
        "cuts_us": cuts or [],
        "cut_history_us": history,
    }
    atomic_write(snapshot, json.dumps(state, ensure_ascii=False, indent=2))
