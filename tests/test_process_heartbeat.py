import os
import sys
import tempfile
import time
from pathlib import Path
from threading import Event, Timer

import pytest

from sculptcut.media import Cancelled, _cleanup_process_directory, run_process


@pytest.mark.skipif(os.name != "nt", reason="Windows inherited file handle sharing")
def test_process_cleanup_waits_for_real_windows_log_handle(tmp_path: Path) -> None:
    temporary = tempfile.TemporaryDirectory(dir=tmp_path)
    directory = Path(temporary.name)
    handle = (directory / "stderr").open("w+b")
    released = Timer(0.2, handle.close)
    released.start()
    try:
        _cleanup_process_directory(temporary)
        assert not directory.exists()
    finally:
        released.join()
        handle.close()
        temporary.cleanup()


@pytest.mark.skipif(os.name != "nt", reason="Windows inherited file handle sharing")
def test_process_cleanup_reports_persistent_windows_log_lock(tmp_path: Path) -> None:
    temporary = tempfile.TemporaryDirectory(dir=tmp_path)
    directory = Path(temporary.name)
    handle = (directory / "stderr").open("w+b")
    try:
        with pytest.raises(OSError):
            _cleanup_process_directory(temporary)
        assert directory.exists()
    finally:
        handle.close()
        temporary.cleanup()


def test_silent_process_heartbeat_can_cancel_live_work(tmp_path: Path) -> None:
    event = Event()
    started = time.monotonic()
    ready = tmp_path / "child-started"

    def heartbeat() -> None:
        if ready.exists():
            event.set()

    with pytest.raises(Cancelled):
        run_process(
            [
                sys.executable,
                "-c",
                "import sys,time; from pathlib import Path; "
                "Path(sys.argv[1]).write_text('ready'); time.sleep(10)",
                str(ready),
            ],
            event,
            heartbeat=heartbeat,
        )
    assert ready.exists()
    assert event.is_set()
    assert time.monotonic() - started < 5
