"""Bound OpenCV's worker pool for small sequential analysis frames."""

import logging
import os

import cv2

# Measurements on 960px proxies favour a small pool over one worker per CPU.
ANALYSIS_THREADS = 4


def configure_analysis_threads() -> None:
    threads = min(ANALYSIS_THREADS, os.cpu_count() or 1)
    cv2.setNumThreads(threads)
    logging.getLogger("artlapse").info("CPU analysis worker threads: %s", threads)
