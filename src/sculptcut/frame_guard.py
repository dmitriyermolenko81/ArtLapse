"""Suppress brief screen/menu flashes without shifting the output timeline."""

import logging
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import cv2
import numpy as np

from sculptcut.media import Cancelled, MediaError
from sculptcut.screen_cleanup import screen_kind


@dataclass(frozen=True)
class FlashSettings:
    max_fraction: float = 0.05
    max_seconds: float = 0.15


SETTINGS = FlashSettings()


def flash_filter(source: Path, fps: int, cancel: Event | None,
                 settings: FlashSettings = SETTINGS) -> tuple[str, list[int]]:
    if fps <= 0 or not 0 <= settings.max_fraction <= 1 or settings.max_seconds < 0:
        raise ValueError('Invalid flash repair settings')
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
    # A screen classifier on cropped/encoded output is only a candidate detector.
    # Long runs can be legitimate sculpture or tool labels, not removable flashes.
    candidates = rejected
    rejected = []
    first = 0
    while first < len(candidates):
        last = first+1
        while last < len(candidates) and candidates[last] == candidates[last-1]+1:
            last += 1
        if candidates[first] > 0 and candidates[last-1] < count-1:
            if last-first <= fps*settings.max_seconds:
                rejected.extend(candidates[first:last])
        first = last
    if len(rejected) > count*settings.max_fraction:
        rejected = []
    if len(candidates) != len(rejected):
        logging.getLogger('artlapse').warning(
            'Screen flash check: preserving %s uncertain frames; repairing %s of %s frames',
            len(candidates)-len(rejected), len(rejected), count)

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
