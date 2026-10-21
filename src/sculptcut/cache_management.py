"""Versioned dependency graph and removal of stale analysis keys only."""

from pathlib import Path

from sculptcut.analysis import analysis_key
from sculptcut.domain import Project
from sculptcut.project import cache_key, database
from sculptcut.proxy import proxy_key

DAG = {
    "proxy": {"version": 1, "parents": ["source"], "settings": ["proxy_width", "proxy_fps"]},
    "features": {"version": 2, "parents": ["proxy", "roi"], "settings": []},
    "motion": {"version": 2, "parents": ["features"], "settings": []},
    "activity": {"version": 2, "parents": ["features", "motion"], "settings": []},
    "plan": {
        "version": 1,
        "parents": ["activity", "overrides"],
        "settings": ["target", "style", "crop", "music"],
    },
    "render": {"version": 1, "parents": ["plan", "originals"], "settings": ["encoder"]},
}


def cleanup_stale(root: Path, project: Project) -> dict[str, int]:
    """Keep current feature keys and all renders/user files. No recursive deletion."""
    connection = database(root)
    removed_rows = removed_files = 0
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        with connection:
            for source in project.sources:
                keys = {
                    "features": analysis_key(project, source),
                    "motion": cache_key("motion-2", analysis_key(project, source)),
                    "activity": cache_key(
                        "activity-3-perceptual-stability", analysis_key(project, source)
                    ),
                }
                for table, key in keys.items():
                    if table in tables:
                        cursor = connection.execute(
                            f"DELETE FROM {table} WHERE source_id=? AND key<>?", (source.id, key)
                        )
                        removed_rows += cursor.rowcount
                expected = f"{source.id}-{proxy_key(project, source)[:16]}.mp4"
                directory = (root / "proxy").resolve()
                for path in directory.glob(f"{source.id}-*.mp4"):
                    if (
                        path.name != expected
                        and not path.is_symlink()
                        and path.resolve().parent == directory
                    ):
                        path.unlink()
                        removed_files += 1
    finally:
        connection.close()
    return {"rows_removed": removed_rows, "proxy_files_removed": removed_files}
