from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from sculptcut.project import create_empty_project
from sculptcut.project_library import merge_projects, previous_settings, project_paths


def ini(path):
    return QSettings(str(path), QSettings.Format.IniFormat)


def test_merge_imports_all_projects_once_without_changing_old_settings(tmp_path):
    old = ini(tmp_path / "old.ini")
    shared = ini(tmp_path / "shared.ini")
    paths = [str(tmp_path / f"project-{i}") for i in range(15)]
    old.setValue("recent_projects", paths + [paths[0].upper()])
    old.sync()
    original = (tmp_path / "old.ini").read_bytes()
    merge_projects(shared, old)
    assert project_paths(shared) == paths
    shared.setValue("recent_projects", paths[1:])
    shared.sync()
    merge_projects(shared, old)
    assert project_paths(shared) == paths[1:]  # no deleted entry resurrection
    assert (tmp_path / "old.ini").read_bytes() == original
    new = str(tmp_path / "добавлен в старой версии")
    old.setValue("recent_projects", [new, *reversed(paths)])
    old.sync()
    merge_projects(shared, old)
    assert project_paths(shared) == [*paths[1:], new]


def test_neighbouring_releases_are_discovered_without_searching_projects(tmp_path, monkeypatch):
    from sculptcut import project_library

    root = tmp_path / "ArtLapse-new/ArtLapse"
    root.mkdir(parents=True)
    old = tmp_path / "ArtLapse-old/ArtLapse/data/settings.ini"
    old.parent.mkdir(parents=True)
    old.write_text("[General]\n")
    unrelated = tmp_path / "private/data/settings.ini"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("[General]\n")
    monkeypatch.setattr(project_library, "portable_root", lambda: root)
    assert previous_settings() == [old]


def test_new_extraction_shows_projects_from_previous_window(tmp_path, monkeypatch):
    from sculptcut import ui

    app = QApplication.instance() or QApplication([])
    shared = tmp_path / "profile/projects.ini"

    def settings(*args):
        if args == ("ArtLapse", "ProjectLibrary"):
            return ini(shared)
        return (
            ini(args[0])
            if len(args) > 1 and args[1] == QSettings.Format.IniFormat
            else (ini(tmp_path / "legacy.ini"))
        )

    monkeypatch.setattr(ui, "QSettings", settings)
    monkeypatch.delenv("ARTLAPSE_DATA_DIR", raising=False)
    monkeypatch.delenv("SCULPTCUT_DATA_DIR", raising=False)
    project = tmp_path / "Работа"
    create_empty_project(project)
    for version in ("first", "new-extraction"):
        monkeypatch.setattr(ui, "data_directory", lambda v=version: tmp_path / v / "data")
        window = ui.MainWindow()
        window.show()
        app.processEvents()
        try:
            if version == "first":
                window.remember_project(project)
            else:
                assert window.recent_project_paths() == [project]
                window.open_project_folder(project)
                assert window.root == project
        finally:
            window._allow_close = True
            window.close()
            app.processEvents()
