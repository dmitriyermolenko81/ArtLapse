from pathlib import Path
from threading import Event

import pytest

from sculptcut import motion, screen_cleanup
from sculptcut.media import Cancelled, discover, run_process
from sculptcut.planning import simple_speed_plan
from sculptcut.project import create_project, database
from sculptcut.proxy import make_proxies


def test_selected_screens_resume_completed_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "screens.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=128x96:r=30:d=8",
            "-vf",
            "drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='lt(t,4)'",
            "-c:v",
            "libx264",
            str(video),
        ]
    )
    root = tmp_path / "project"
    project = create_project(root, [video])
    make_proxies(root, project)
    base = simple_speed_plan(project, 2)
    first = base.decisions[0]
    plan = base.model_copy(
        update={
            "decisions": [
                first.model_copy(update={"source_end_us": 2_000_000, "output_frame_count": 30}),
                first.model_copy(
                    update={
                        "source_start_us": 2_000_000,
                        "source_end_us": 6_000_000,
                        "action": "DROP",
                        "output_frame_count": 0,
                    }
                ),
                first.model_copy(update={"source_start_us": 6_000_000, "output_frame_count": 30}),
            ]
        }
    )
    monkeypatch.setattr(
        screen_cleanup,
        "DEFAULT_SCREENS",
        screen_cleanup.DEFAULT_SCREENS.model_copy(update={"cache_window_seconds": 1}),
    )
    cancel = Event()
    with pytest.raises(Cancelled):
        screen_cleanup.project_screens(root, project, cancel, plan, lambda _: cancel.set())
    with database(root) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM cache WHERE stage='screen_window'").fetchone()[
                0
            ]
            == 1
        )
    original = screen_cleanup.screen_kind
    calls = []

    def counted(gray):  # type: ignore[no-untyped-def]
        calls.append(1)
        return original(gray)

    monkeypatch.setattr(screen_cleanup, "screen_kind", counted)
    resumed = screen_cleanup.project_screens(root, project, None, plan)
    assert len(calls) == 15  # Completed 5 frames were cached; DROP's 20 frames never scanned.
    assert len(resumed) == 1 and resumed[0].end_us == 2_200_000
    assert screen_cleanup.project_screens(root, project, None, plan) == resumed
    assert len(calls) == 15
    full = screen_cleanup.project_screens(root, project, None)
    assert full[0].end_us > resumed[0].end_us


def test_motion_resumes_without_recomputing_saved_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "motion.mp4"
    run_process(
        [
            discover("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=128x96:r=30:d=3",
            "-c:v",
            "libx264",
            str(video),
        ]
    )
    root = tmp_path / "project"
    project = create_project(root, [video])
    make_proxies(root, project)
    original = motion.estimate_motion
    cancel = Event()
    calls = []

    def interrupted(*args):  # type: ignore[no-untyped-def]
        value = original(*args)
        calls.append(1)
        if len(calls) == 4:
            cancel.set()
        return value

    monkeypatch.setattr(motion, "estimate_motion", interrupted)
    with pytest.raises(Cancelled):
        motion.analyze_motion(root, project, cancel)
    calls.clear()

    def counted(*args):  # type: ignore[no-untyped-def]
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(motion, "estimate_motion", counted)
    motion.analyze_motion(root, project)
    assert len(calls) == 10  # Four saved frame-pair estimates were not recomputed.
    monkeypatch.setattr(motion, "estimate_motion", original)
    with database(root) as connection:
        resumed = connection.execute(
            "SELECT timestamp,value FROM motion ORDER BY timestamp"
        ).fetchall()
        connection.execute("DELETE FROM motion")
        connection.execute("DELETE FROM cache WHERE stage='motion'")
    motion.analyze_motion(root, project)
    with database(root) as connection:
        fresh = connection.execute(
            "SELECT timestamp,value FROM motion ORDER BY timestamp"
        ).fetchall()
    assert len(resumed) == 15 and resumed == fresh
