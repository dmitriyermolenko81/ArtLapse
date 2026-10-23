# SPDX-License-Identifier: GPL-3.0-or-later
"""Download the official toolchain in resumable ranges, then verify its release digest."""

import concurrent.futures
import hashlib
import os
import sys
import time
import urllib.request
from pathlib import Path

URL = (
    "https://github.com/mstorsjo/llvm-mingw/releases/download/20261020/"
    "llvm-mingw-20261020-ucrt-x86_64.zip"
)
SHA256 = "fd5462b7f5d93b2ff981fcd3f9c7e427e47f3203c64ea91c27bcc444eebd74cd"
SIZE = 190612575
CHUNK = 1024 * 1024


def main() -> None:
    root = Path(__file__).resolve().parents[2] / "artifacts/ffmpeg-own-202/downloads"
    url, expected, size, filename = URL, SHA256, SIZE, "llvm-verified.zip"
    source = "--llvm-source" in sys.argv[1:]
    if source:
        url = (
            "https://github.com/llvm/llvm-project/releases/download/llvmorg-23.1.4/"
            "llvm-project-23.1.4.src.tar.xz"
        )
        expected = "819ecf7351f1a66f76b2d701d8942faaf5faeb324cb90c9d5b090de142434bb2"
        size, filename = 179272572, "llvm-project-23.1.4.src.tar.xz"
    parts = root / ("llvm-source-parts" if source else "llvm-parts")
    parts.mkdir(exist_ok=True)

    def fetch(index: int) -> None:
        start = index * CHUNK
        end = min(size, start + CHUNK) - 1
        target = parts / f"{index:04d}"
        if target.exists() and target.stat().st_size == end - start + 1:
            return
        for attempt in range(4):
            try:
                request = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
                with urllib.request.urlopen(request, timeout=25) as response:
                    if response.status != 206:
                        raise ValueError(f"Expected partial response, got {response.status}")
                    if response.headers["Content-Range"] != f"bytes {start}-{end}/{size}":
                        raise ValueError("Unexpected content range")
                    data = response.read(CHUNK + 1)
                if len(data) != end - start + 1:
                    raise ValueError("Incomplete range")
                target.write_bytes(data)
                print(f"range {index + 1}/{(size + CHUNK - 1) // CHUNK} OK", flush=True)
                return
            except (OSError, ValueError):
                if attempt == 3:
                    raise
                time.sleep(1)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(fetch, range((size + CHUNK - 1) // CHUNK)))
    target = root / filename
    digest = hashlib.sha256()
    with target.open("wb") as output:
        for index in range((size + CHUNK - 1) // CHUNK):
            data = (parts / f"{index:04d}").read_bytes()
            output.write(data)
            digest.update(data)
    if digest.hexdigest() != expected:
        raise ValueError(f"Toolchain SHA256 mismatch: {digest.hexdigest()}")
    os.chmod(target, 0o644)
    print(f"SHA256 VERIFIED {expected}", flush=True)


if __name__ == "__main__":
    main()
