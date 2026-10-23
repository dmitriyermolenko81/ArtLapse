# SPDX-License-Identifier: GPL-3.0-or-later
"""Account for every change to archived library source files used by this build."""

import difflib
import json
import tarfile
from pathlib import Path

from prepare_inputs import digest

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "artifacts/ffmpeg-own-202"
INPUTS = ROOT / "packaging/ffmpeg/inputs.lock.json"


def main() -> None:
    patched = {
        "ffmpeg-8.1.2/ffbuild/version.sh": "ffmpeg-release-version.patch",
        "ffmpeg-8.1.2/libavfilter/vsrc_gfxcapture_winrt.cpp": "ffmpeg-system-error.patch",
        "ffmpeg-8.1.2/ffbuild/library.mak": "ffmpeg-native-response.patch",
        "dav1d-1.5.3/meson.build": "dav1d-relative-nasm.patch",
    }
    relevant = ("ffmpeg-", "x264-", "libvpx-", "opus-", "zlib-", "dav1d-", "nv-codec-")
    result: dict[str, object] = {}
    seen: set[str] = set()
    for entry in json.loads(INPUTS.read_text(encoding="utf-8"))["inputs"]:
        if not entry["file"].startswith(relevant):
            continue
        archive = WORK / "downloads" / entry["file"]
        assert digest(archive) == entry["sha256"]
        unchanged = 0
        differences = []
        with tarfile.open(archive, "r|*") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                stream = tar.extractfile(member)
                assert stream is not None
                original = stream.read()
                current = WORK / "sources" / member.name
                if member.name == "ffmpeg-8.1.2/VERSION":
                    assert not current.exists()
                    assert (current.parent / "FFMPEG_RELEASE_VERSION").read_bytes() == original
                    differences.append(
                        {
                            "file": member.name,
                            "change": "release metadata renamed",
                            "patch": "ffmpeg-release-version.patch",
                        }
                    )
                    continue
                actual = current.read_bytes()
                if actual == original:
                    unchanged += 1
                    continue
                assert member.name in patched, f"Undocumented source change: {member.name}"
                relative = "/".join(Path(member.name).parts[1:])
                expected = "".join(
                    difflib.unified_diff(
                        original.decode("utf-8").splitlines(keepends=True),
                        actual.decode("utf-8").replace("\r\n", "\n").splitlines(keepends=True),
                        "a/" + relative,
                        "b/" + relative,
                        n=0,
                    )
                )
                patch = ROOT / "packaging/ffmpeg/patches" / patched[member.name]
                assert expected in patch.read_text(encoding="utf-8"), member.name
                differences.append({"file": member.name, "patch": patch.name})
                seen.add(member.name)
        result[entry["file"]] = {"unchanged_source_files": unchanged, "changes": differences}
    assert seen == set(patched), set(patched) - seen
    output = {
        "status": "PASS",
        "libraries": result,
        "generated_build_configuration": "Recorded separately; original inputs retained",
    }
    (WORK / "source-correspondence.json").write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
