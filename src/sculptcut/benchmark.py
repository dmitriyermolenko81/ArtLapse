"""Local resource measurements; native process memory on Windows."""

import ctypes
import os
import time
import tracemalloc
from pathlib import Path

from sculptcut.analysis import extract_features
from sculptcut.domain import Project
from sculptcut.project import atomic_write


def peak_working_set() -> int | None:
    if os.name != "nt":
        return None

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("faults", ctypes.c_ulong),
            ("peak", ctypes.c_size_t),
            ("working", ctypes.c_size_t),
            ("peak_paged", ctypes.c_size_t),
            ("paged", ctypes.c_size_t),
            ("peak_nonpaged", ctypes.c_size_t),
            ("nonpaged", ctypes.c_size_t),
            ("pagefile", ctypes.c_size_t),
            ("peak_pagefile", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    api = ctypes.WinDLL("psapi", use_last_error=True)
    api.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    if not api.GetProcessMemoryInfo(
        kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    ):
        return None
    return int(counters.peak)


def benchmark(root: Path, project: Project) -> dict[str, float | int | str | None]:
    wall, cpu = time.perf_counter(), time.process_time()
    tracemalloc.start()
    try:
        extract_features(root, project)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    result: dict[str, float | int | str | None] = {
        "wall_seconds": time.perf_counter() - wall,
        "cpu_seconds": time.process_time() - cpu,
        "python_heap_peak_bytes": peak,
        "process_peak_working_set_bytes": peak_working_set(),
        "project_disk_bytes": sum(p.stat().st_size for p in root.rglob("*") if p.is_file()),
        "scope": "Analysis process only; FFmpeg child memory excluded. Warm cache may be reused.",
    }
    import json

    atomic_write(root / "logs" / "benchmark.json", json.dumps(result, indent=2))
    return result
