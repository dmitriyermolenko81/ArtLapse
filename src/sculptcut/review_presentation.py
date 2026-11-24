"""Edit the presentation of a reviewed montage by reusing its encoded video."""

import json
import tempfile
from pathlib import Path
from threading import Event
from typing import Literal

from sculptcut.audio import add_music
from sculptcut.domain import Project, ProjectSettings
from sculptcut.export_storage import metadata_path, publish_export
from sculptcut.intro import active_intro_image, intro_signature, output_frame_count
from sculptcut.media import discover, probe, run_process
from sculptcut.music_edit import (
    assemble_music,
    draft_path,
    music_edit_locked,
    mux_music,
    save_draft,
)
from sculptcut.output_cuts import saved_soundtrack
from sculptcut.output_naming import next_variant_path
from sculptcut.planning import RenderPlan
from sculptcut.presentation import attach_presentation, presentation_frames
from sculptcut.project import atomic_write, fingerprint
from sculptcut.remux_check import video_samples
from sculptcut.shorts_refill import cuts_on_plan_clock
from sculptcut.track_edit import audible_clips, saved_music_clips
from sculptcut.variants import cut_history, read_variant, save_variant


def review_presentation_settings(video: Path) -> ProjectSettings | None:
    state = read_variant(video)
    if not state.get("plan") or not state.get("project"):
        return None
    settings = Project.model_validate(state["project"]).settings
    return settings if active_intro_image(settings) is None else None


def update_review_presentation(root: Path, video: Path, clip: Path | None, seconds: float,
                               position: Literal["start", "end"], cancel: Event) -> Path:
    state = read_variant(video)
    if state.get("video_fingerprint") and state["video_fingerprint"] != fingerprint(video):
        raise ValueError("Файл просмотренного ролика изменился.")
    project = Project.model_validate(state["project"])
    old = project.settings
    if clip is None and old.presentation_video is None:
        raise ValueError("В просмотренном ролике нет презентации.")
    history = cut_history(video)
    if active_intro_image(old):
        raise ValueError("Сначала выберите версию без подрезанной заставки.")
    plan = RenderPlan.model_validate(state["plan"])
    cuts = cuts_on_plan_clock(video, project, plan, []) if (
        history or state.get("bookend_cuts_us")) else []
    settings = old.model_copy(update={"presentation_video": clip, "intro_image": None,
                                      "presentation_seconds": seconds,
                                      "presentation_position": position})
    project = project.model_copy(update={"settings": settings})
    fps = plan.output_fps
    extra = presentation_frames(settings, fps)
    previous = presentation_frames(old, fps)
    old_head = previous if old.presentation_position == "start" else 0
    new_head = extra if position == "start" else 0
    body_end = old_head + plan.actual_frame_count
    body_cuts = []
    for a, b in cuts:
        left, right = round(a * fps / 1e6), round(b * fps / 1e6)
        previous -= max(0, min(right, old_head) - left) + max(0, right - max(left, body_end))
        start, stop = max(left, old_head), min(right, body_end)
        if stop > start:
            body_cuts.append((round((start-old_head+new_head)*1e6/fps),
                              round((stop-old_head+new_head)*1e6/fps)))
    history = [body_cuts] if body_cuts else []
    original_count = video_samples(video, cancel)
    count = original_count - previous + extra
    info = probe(video, cancel)
    duration = round(count * 1_000_000 / fps)
    draft_settings = dict(plan.settings)
    draft_settings.pop("request_key", None)
    if not draft_settings.get("draft") and video.parent == root / "renders":
        draft_settings["preview_quality"] = "original"
    draft_settings.update(draft=1, intro_fingerprint=intro_signature(settings),
                          actual_output_frames=output_frame_count(settings, fps,
                                                                 plan.actual_frame_count))
    plan = plan.model_copy(update={"settings": draft_settings})
    clips = saved_music_clips(video)
    if clips and music_edit_locked(video):
        # The soundtrack follows the body in rhythm presets. The editable
        # music lane in other presets keeps its own absolute clock.
        shift = round((new_head - (previous if old_head else 0)) * 1e6 / fps)
        clips = [item.model_copy(update={
            "at_us": max(0, item.at_us + shift),
            "start_us": item.start_us + max(0, -item.at_us - shift),
        }) for item in clips if item.at_us + item.end_us - item.start_us + shift > 0]
    music = saved_soundtrack(video)
    track = music[0] if music else None
    if clips is not None:
        track = assemble_music(root, audible_clips(clips, duration), duration, cancel)
    if info.audio_streams and track is None:
        from sculptcut.music_edit import embedded_music

        clips = embedded_music(video, info)
        track = assemble_music(root, audible_clips(clips, duration), duration, cancel)
    folder = root / "previews"
    folder.mkdir(parents=True, exist_ok=True)
    output = next_variant_path(folder, "presentation", settings.preset)
    lane = metadata_path(output, ".music-edit.json")
    with tempfile.TemporaryDirectory(dir=folder) as scratch:
        work = Path(scratch)
        body = video
        if previous:
            from sculptcut.smart_cut import splice_video

            start = previous if old.presentation_position == "start" else 0
            end = start + original_count - previous
            ranges = [(round(start * 1e6 / fps), round(end * 1e6 / fps))]
            trimmed = splice_video(video, work, ranges, info, cancel)
            if trimmed is None:
                trimmed = work / "body.mp4"
                run_process([discover("ffmpeg"), "-v", "error", "-xerror", "-ss",
                             f"{start/fps:.6f}", "-i", str(video), "-map", "0:v:0", "-an",
                             "-frames:v", str(end-start), "-c:v", "libx264", "-crf", "18",
                             str(trimmed)], cancel)
            body = trimmed
        ready = body
        if clip is not None:
            ready = work / "presentation.mp4"
            attach_presentation(body, ready, settings, width=info.width, height=info.height,
                                fps=fps, encoder="libx264", cancel=cancel)
        if track:
            mixed = Path(scratch) / "sound.mp4"
            if clips is not None:
                mux_music(ready, track, mixed, duration, cancel)
            else:
                add_music(ready, track, mixed, duration / 1e6,
                          settings.music_loop, settings.music_fade_seconds, cancel)
            ready = mixed
        if ready == video:
            # The old presentation may already be gone through ordinary cuts.
            ready = work / "body.mp4"
            run_process([discover("ffmpeg"), "-v", "error", "-xerror", "-i", str(video),
                         "-map", "0", "-c", "copy", str(ready)], cancel)
        if video_samples(ready, cancel) != count:
            raise ValueError("Длительность презентации не совпала с монтажом.")
        with publish_export(ready, output, cancel, extra_files=(lane, draft_path(root, output))):
            save_variant(output, ready=ready, project=project, plan=plan, parent=video,
                         operation="presentation",
                         music=track, history=history,
                         episode_base_plan=RenderPlan.model_validate(state["episode_base_plan"])
                         if state.get("episode_base_plan") else None)
            if clips is not None:
                save_draft(root, output, clips, ready=ready)
                atomic_write(lane, json.dumps([c.model_dump(mode="json") for c in clips],
                                              ensure_ascii=False))
    return output
