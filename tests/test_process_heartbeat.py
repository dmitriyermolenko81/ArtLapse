import sys
import time
from pathlib import Path
from threading import Event

import pytest

from sculptcut.media import Cancelled, run_process


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
