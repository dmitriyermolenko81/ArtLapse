"""Alerts exclude their episode, including the shrinking animation tail."""

from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
from test_timeline import make_source

import sculptcut.work_episodes as work
from sculptcut.domain import Project
from sculptcut.overlay_guard import notification_overlay
from sculptcut.planning import build_plan
from sculptcut.project import create_project
from sculptcut.proxy import make_proxies
from sculptcut.screen_cleanup import project_screens, screen_kind
from sculptcut.segmentation import Segment


def scene(alert=False, popup=False):
    image = np.full((360, 640), 100, np.uint8)
    cv2.ellipse(image, (140, 210), (55, 95), 0, 0, 360, 160, -1)
    cv2.circle(image, (125, 190), 8, 240, -1)
    if alert:
        cv2.ellipse(image, (345, 130), (50, 65), 0, 0, 360, 245, -1)
        cv2.ellipse(image, (345, 130), (46, 61), 0, 0, 360, 150, -1)
        cv2.putText(image, "Welcome artist", (270, 220), 0, 0.5, 245, 1)
        cv2.putText(image, "Thanks for joining", (260, 240), 0, 0.5, 245, 1)
    if popup:
        cv2.rectangle(image, (220, 100), (500, 220), 30, -1)
        cv2.putText(image, "New notification", (240, 140), 0, 0.5, 245, 1)
        cv2.putText(image, "Processing complete", (240, 175), 0, 0.5, 245, 1)
    return image


def test_alert_and_popup_are_rejected_but_a_clean_model_is_not() -> None:
    assert notification_overlay(scene(alert=True))
    assert notification_overlay(scene(popup=True))
    assert screen_kind(scene(alert=True)) == "notification_overlay"
    assert not notification_overlay(scene())


def test_real_proxy_excludes_complete_alert_episode(tmp_path: Path) -> None:
    video = tmp_path / "alert.mp4"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 10, (640, 360))
    assert writer.isOpened()
    for i in range(40):
        writer.write(cv2.cvtColor(scene(alert=10 <= i < 30), cv2.COLOR_GRAY2BGR))
    writer.release()
    root = tmp_path / "project"
    project = create_project(root, [video])
    make_proxies(root, project)
    plan = build_plan(project, [], 2)
    ranges = project_screens(root, project, None, plan)
    assert any(r.reason == "notification_overlay" and r.start_us == 0
               and r.end_us == 4_000_000 for r in ranges)


def test_more_scenes_and_moderate_speed_fit_the_same_budget(monkeypatch) -> None:
    project = Project(sources=[make_source(0, 0, 1200_000_000)])
    base = build_plan(project, [], 60)
    pool = work.detect_episodes(project, base, [Segment(
        source_id="0", start_us=0, end_us=1200_000_000, kind="SCULPT", score=0.9,
    )])
    new = work.highlight_plan(project, base, pool)
    monkeypatch.setattr(work, "SETTINGS", replace(work.SETTINGS,
        preferred_output_seconds=1.6, preferred_speed=2))
    old = work.highlight_plan(project, base, pool)
    assert new.actual_frame_count == old.actual_frame_count == 1800
    assert new.settings["work_selected_count"] > old.settings["work_selected_count"]
    assert all(d.output_frame_count <= 90 for d in new.decisions)
