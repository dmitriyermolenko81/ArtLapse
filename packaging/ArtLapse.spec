# Run with python -m PyInstaller packaging/ArtLapse.spec
from pathlib import Path
from importlib.metadata import distribution
import json
import sys
from urllib.parse import urlsplit
from PySide6.QtCore import QLibraryInfo
import runpy

root = Path(SPECPATH).parent
excluded_binary = runpy.run_path(str(root / "packaging/release_policy.py"))["excluded_binary"]
feedback_config = root / "packaging" / "feedback-config.json"
if feedback_config.is_file():
    config = json.loads(feedback_config.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or set(config) != {"endpoint"}:
        raise ValueError("feedback-config.json may contain only a public endpoint")
    endpoint = urlsplit(config["endpoint"])
    if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username
            or endpoint.password or endpoint.query or endpoint.fragment
            or endpoint.hostname == "api.telegram.org"):
        raise ValueError("Feedback config must contain a public HTTPS receiver URL, not bot keys")
datas = [(str(root / "README.md"), "."),
         (str(root / "LICENSE"), "."),
         (str(root / "docs/LIBRARY_REPLACEMENT.md"), "docs"),
         (str(root / "docs/DATA_AND_FEEDBACK.md"), "docs"),
         (str(root / "src" / "sculptcut" / "assets" / "logo.png"), "sculptcut/assets"),
         (str(root / "src" / "sculptcut" / "assets" / "logo.ico"), "sculptcut/assets"),
         (str(root / "src" / "sculptcut" / "assets" / "chevron-down.svg"), "sculptcut/assets"),
         (str(root / "src" / "sculptcut" / "assets" / "en.json"), "sculptcut/assets"),
         (str(root / "packaging" / "THIRD_PARTY_NOTICES.md"), ".")]
if feedback_config.is_file():
    datas.append((str(feedback_config), "sculptcut/assets"))
translations = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))
for name in ("qtbase_ru.qm", "qtmultimedia_ru.qm"):
    datas.append((str(translations / name), "PySide6/translations"))
for name in ("pydantic", "pydantic-core", "annotated-types", "typing-extensions",
             "typing-inspection", "numpy", "opencv-python-headless", "PySide6",
             "PySide6-Essentials", "PySide6-Addons", "shiboken6", "pyinstaller"):
    package = distribution(name)
    for file in package.files or []:
        if any(word in file.name.upper() for word in ("LICENSE", "COPYING")) or file.name == "METADATA":
            datas.append((str(package.locate_file(file)), f"licenses/{name}/{file.parent}"))
python_license = Path(sys.base_prefix) / "LICENSE.txt"
if not python_license.is_file():
    raise RuntimeError("Python license is required")
datas.append((str(python_license), "licenses/python"))
extra_licenses = root / "packaging" / "licenses"
if extra_licenses.exists():
    datas.append((str(extra_licenses), "licenses/upstream"))
a = Analysis([str(root / "packaging" / "launcher.py")], pathex=[str(root / "src")],
             binaries=[], datas=datas, hiddenimports=[], hookspath=[],
             excludes=["pytest", "mypy", "ruff", "tkinter", "PySide6.QtVirtualKeyboard"],
             noarchive=False)
# The Qt hook can collect the input-context plugin even without a Python import.
a.binaries = [entry for entry in a.binaries if not excluded_binary(entry[0])]
pyz = PYZ(a.pure)
gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name="ArtLapse",
          console=False, debug=False, strip=False, upx=False,
          icon=str(root / "packaging" / "ArtLapse.ico"))
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="artlapse-cli",
          console=True, debug=False, strip=False, upx=False,
          icon=str(root / "packaging" / "ArtLapse.ico"))
coll = COLLECT(gui, cli, a.binaries, a.datas, strip=False, upx=False, name="ArtLapse")
