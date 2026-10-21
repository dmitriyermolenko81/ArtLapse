"""Suppress brief screen/menu flashes without shifting the output timeline."""

from pathlib import Path
from threading import Event

import cv2
import numpy as np

from sculptcut.media import Cancelled, MediaError
from sculptcut.screen_cleanup import screen_kind

MAX_FLASH_FRACTION = 0.05


def flash_filter(source: Path, fps: int, cancel: Event | None) -> tuple[str, list[int]]:
    capture = cv2.VideoCapture(str(source))
    rejected: list[int] = []
    count = 0
    try:
        while True:
            if cancel and cancel.is_set():
                raise Cancelled("Screen flash check cancelled")
            ok, frame = capture.read()
            if not ok:
                break
            if screen_kind(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.uint8)):
                rejected.append(count)
            count += 1
    finally:
        capture.release()
    if not count:
        raise MediaError("Cannot decode screen flash check input")
    if len(rejected) > count * MAX_FLASH_FRACTION:
        raise MediaError("Too many menu frames for flash repair; rebuild the edit plan")
    clock = f"settb=1/{fps},setpts=N"
    if not rejected:
        return clock, rejected
    selected = "+".join(f"eq(n,{index})" for index in rejected)
    # Keep timestamps of clean frames. FPS fills only the vacated slots with a
    # neighbour; it does not compress the timeline or alter soundtrack alignment.
    return (
        f"{clock},select='not({selected})',fps={fps}:start_time=0,"
        f"tpad=stop_mode=clone:stop_duration={count / fps:.9f},"
        f"trim=end_frame={count},{clock}",
        rejected,
    )
