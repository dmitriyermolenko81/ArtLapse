"""Bounded, prefetched A/V windows for playing an edited montage from its cursor."""

import json
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from sculptcut.domain import Project
from sculptcut.episode_context_preview import (
    ContextClip,
    ContextPreparer,
    PreviewAudio,
    PreviewScene,
    shorts_scenes,
)
from sculptcut.episode_playback import EpisodePlayback
from sculptcut.intro import body_start_frame, output_frame_count
from sculptcut.planning import RenderDecision, RenderPlan
from sculptcut.selection_duration import timeline_frames, timeline_start
from sculptcut.shorts_refill import _expand_montage, _split_decision
from sculptcut.timewarp import output_before


def bookend_scenes(video: Path, clock: int, saved: int, frames: int,
                   cuts: list[tuple[int, int]], saved_ranges: list[tuple[int, int]] | None,
                   fps: int, compact: bool = False) -> list[tuple[int, int, PreviewScene]]:
    """Map retained intro/presentation frames onto the saved MP4 clock."""
    entries: list[tuple[int, int, PreviewScene]] = []
    kept = []
    cursor = 0
    for a, b in sorted(cuts):
        if cursor < min(a, frames):
            kept.append((cursor, min(a, frames)))
        cursor = max(cursor, b)
    if cursor < frames:
        kept.append((cursor, frames))
    ranges = saved_ranges if saved_ranges is not None else [(0, saved+frames)]
    kept_frames = 0
    for a, b in kept:
        elapsed = 0
        for left, right in ranges:
            start, end = max(saved+a, left), min(saved+b, right)
            if start < end:
                local = clock+(kept_frames if compact else start-saved)
                entries.append((round(local*1000/fps), round((local+end-start)*1000/fps),
                    PreviewScene(len(entries), video, round((elapsed+start-left)*1000/fps),
                        round((elapsed+end-left)*1000/fps), end-start)))
                kept_frames += end-start
            elapsed += right-left
    return entries


def rendered_segments(project: Project, plan: RenderPlan) -> list[tuple[int, RenderDecision]]:
    clock = body_start_frame(project.settings, plan.output_fps, plan.actual_frame_count)
    result = []
    for part in (p for d in plan.decisions for p in _expand_montage(d)):
        if part.output_frame_count:
            result.append((clock, part))
            clock += part.output_frame_count
    return result


def reused_scene(decision: RenderDecision, video: Path, original: RenderPlan,
                 segments: list[tuple[int, RenderDecision]], row: int,
                 saved_ranges: list[tuple[int, int]] | None = None,
                 retimed: bool = False) -> PreviewScene | None:
    for frame, old in segments:
        if (old.source_id != decision.source_id or old.crop != decision.crop
                or old.subject_camera != decision.subject_camera
                or old.speed_ramp != decision.speed_ramp or old.action != decision.action):
            continue
        if decision.action == "HOLD":
            if old.source_start_us != decision.source_start_us:
                continue
            start, end = 0, decision.output_frame_count
            if end > old.output_frame_count:
                continue
        else:
            if not (old.source_start_us <= decision.source_start_us
                    < decision.source_end_us <= old.source_end_us):
                continue
            if old.subject_camera is not None and (
                    old.source_start_us != decision.source_start_us
                    or old.source_end_us != decision.source_end_us):
                continue
            start = output_before(old, decision.source_start_us)
            end = output_before(old, decision.source_end_us)
            if end <= start:
                continue
            if end-start != decision.output_frame_count and not retimed:
                continue
        left, right = frame+start, frame+end
        if saved_ranges is not None:
            elapsed = 0
            for a, b in saved_ranges:
                if a <= left < right <= b:
                    left, right = elapsed+left-a, elapsed+right-a
                    break
                elapsed += b-a
            else:
                return None
        return PreviewScene(row, video, round(left*1000/original.output_fps),
                            round(right*1000/original.output_fps), decision.output_frame_count)
    return None


class MontagePlayback(QObject):
    minimum_initial_window_ms = 3000
    maximum_window_ms = 6000
    maximum_window_scenes = 12
    clock_changed = Signal(int)
    busy_changed = Signal(bool)
    failed = Signal(str)

    def __init__(self, playback: EpisodePlayback, parent: QObject) -> None:
        super().__init__(parent)
        self.playback = playback
        self.standby = EpisodePlayback(self)
        self.standby.audio.setMuted(True)
        self.standby.image_ready.connect(self.standby_frame)
        self.preparer = ContextPreparer(self)
        self.preparer.ready.connect(self.prepared)
        self.preparer.failed.connect(self.error)
        playback.position_changed.connect(self.position)
        playback.finished.connect(self.finished)
        self.enabled = False
        self.desired = False
        self.entries: list[tuple[int, int, PreviewScene]] = []
        self.current = -1
        self.requested = 0
        self.next_index = 0
        self.cached: tuple[int, ContextClip] | None = None
        self.reserve: list[tuple[int, ContextClip]] = []
        self.prefetching = False
        self.waiting = False
        self.cursor = 0
        self.initial_cursor = 0
        self.episode_only = False
        self.direct = False
        self.direct_offset = 0

    def start(self, project: Project, plan: RenderPlan, cursor: int,
              episode_only: bool = False, video: Path | None = None,
              original_frames: int | None = None, direct: bool = False,
              original: RenderPlan | None = None,
              saved_ranges: list[tuple[int, int]] | None = None,
              reuse_stream: bool = False,
              leading: list[tuple[int, int]] | None = None,
              trailing: list[tuple[int, int]] | None = None) -> None:
        self.stop()
        self.project, self.plan = project, plan
        self.episode_only = episode_only
        self.entries = []
        head = body_start_frame(project.settings, plan.output_fps, plan.actual_frame_count)
        compact = plan.settings.get('selection_duration_policy') == 'fixed'
        clock = timeline_start(project, plan, leading or [])
        if video is not None and head and not episode_only:
            cuts = json.loads(str(plan.settings.get('selection_head', json.dumps(leading or []))))
            self.bookend(video, 0, 0, head, cuts, saved_ranges, plan.output_fps, compact)
        sources = {s.id: s for s in project.sources}
        segments: list[tuple[int, RenderDecision]] = []
        if original is not None and original.output_fps == plan.output_fps:
            segments = rendered_segments(project, original)
        for d in (part for decision in plan.decisions for part in _expand_montage(decision)):
            if d.output_frame_count:
                start = round(clock*1000/plan.output_fps)
                clock += d.output_frame_count
                end = round(clock*1000/plan.output_fps)
                reused = (reused_scene(d, video, original, segments,
                                       len(self.entries), saved_ranges, retimed=True)
                          if video is not None and original is not None else None)
                scene = reused or PreviewScene(len(self.entries),
                    sources[d.source_id].path, d.source_start_us//1000,
                    max(d.source_start_us//1000+1, d.source_end_us//1000),
                    d.output_frame_count, d)
                self.entries.append((start, end, scene))
        raw_total = output_frame_count(project.settings, plan.output_fps, plan.actual_frame_count)
        tail = raw_total-head-plan.actual_frame_count
        total = timeline_frames(project, plan, leading or [], trailing or [])
        if video is not None and original_frames is not None and tail > 0 and not episode_only:
            old_tail = body_start_frame(project.settings, plan.output_fps, original_frames)
            old_tail += original_frames
            cuts = json.loads(str(plan.settings.get('selection_tail', json.dumps(trailing or []))))
            self.bookend(video, clock, old_tail, tail, cuts, saved_ranges, plan.output_fps, compact)
        if not self.entries:
            return
        index = next((i for i, (_, end, _) in enumerate(self.entries) if cursor < end), 0)
        self.initial_cursor = max(self.entries[index][0], min(cursor, self.entries[index][1]-1))
        self.cursor = self.initial_cursor
        self.enabled = self.desired = self.waiting = True
        self.current = -1
        self.cached = None
        # Already-rendered adjoining scenes form one continuous A/V stream.
        # Do not encode windows or restart the decoder at their edit boundaries.
        contiguous = all(s.decision is None and s.path == video for _, _, s in self.entries)
        contiguous = contiguous and all(abs(s.end_ms-s.start_ms-s.frames*1000/plan.output_fps)
                                         <= 1 for _, _, s in self.entries)
        contiguous = contiguous and all(
            abs(left[2].end_ms-right[2].start_ms) <= 1
            and abs(left[1]-right[0]) <= 1
            for left, right in zip(self.entries, self.entries[1:], strict=False))
        saved_ms = (round(sum(b-a for a, b in saved_ranges)*1000/plan.output_fps)
                    if saved_ranges is not None else round(total*1000/plan.output_fps))
        contiguous = (contiguous and self.entries[0][2].start_ms == 0
                      and abs(self.entries[-1][2].end_ms-saved_ms) <= 1)
        if (direct or (reuse_stream and contiguous)) and video is not None:
            self.direct = True
            self.waiting = False
            self.current = index
            self.direct_offset = self.entries[index][2].start_ms-self.entries[index][0]
            self.initial_cursor = max(self.entries[index][0], self.initial_cursor)
            first = self.entries[index][2].start_ms if episode_only else self.entries[0][2].start_ms
            last = self.entries[index][2].end_ms if episode_only else self.entries[-1][2].end_ms
            self.playback.player.setPlaybackRate(1)
            self.playback.limit_frames = True
            self.playback.select(video, first, last, self.initial_cursor+self.direct_offset)
            self.playback.play()
            return
        # Render only the suffix at the cursor, not the beginning of its episode.
        start, end, scene = self.entries[index]
        skip = round((self.initial_cursor-start)*plan.output_fps/1000)
        if 0 < skip < scene.frames:
            if scene.decision is not None:
                part = next(p for p in _split_decision(scene.decision, 0, [(0, skip)])
                            if p.output_frame_count)
                scene = PreviewScene(scene.row, scene.path, part.source_start_us//1000,
                    max(part.source_start_us//1000+1, part.source_end_us//1000),
                    part.output_frame_count, part)
            else:
                scene = PreviewScene(scene.row, scene.path,
                    scene.start_ms+round(skip*(scene.end_ms-scene.start_ms)/scene.frames),
                    scene.end_ms,
                    scene.frames-skip)
            self.entries[index] = self.initial_cursor, end, scene
        self.queue(index)

    def bookend(self, video: Path, clock: int, saved: int, frames: int,
                cuts: list[tuple[int, int]], saved_ranges: list[tuple[int, int]] | None,
                fps: int, compact: bool = False) -> None:
        scenes = bookend_scenes(video, clock, saved, frames, cuts, saved_ranges, fps, compact)
        for start, end, scene in scenes:
            self.entries.append((start, end, PreviewScene(len(self.entries), scene.path,
                scene.start_ms, scene.end_ms, scene.frames)))

    def queue(self, index: int) -> None:
        self.prefetching = True
        self.requested = index
        end = index+1
        if not self.episode_only and self.current < 0:
            while (end < min(len(self.entries), index+self.maximum_window_scenes)
                   and self.entries[end-1][1]-self.entries[index][0]
                   < self.minimum_initial_window_ms
                   and self.entries[end][2].decision is None):
                end += 1
        if not self.episode_only and self.current >= 0:
            # Do not prepare six seconds / twelve separate encodes while the
            # current window gives us only a second to finish that work.
            budget = min(self.maximum_window_ms, max(self.minimum_initial_window_ms,
                self.entries[index-1][1]-self.entries[self.current][0]))
            while (end < min(len(self.entries), index+self.maximum_window_scenes)
                   and (self.entries[end-1][1]-self.entries[index][0]
                        < self.minimum_initial_window_ms
                        or self.entries[end][1]-self.entries[index][0] <= budget)):
                end += 1
        self.next_index = end
        settings = self.project.settings
        total = timeline_frames(self.project, self.plan, [], [])
        audio = (PreviewAudio(settings.music_path, self.entries[index][0]/1000,
                 total/self.plan.output_fps, settings.music_loop, settings.music_fade_seconds)
                 if settings.music_path else None)
        self.busy_changed.emit(self.waiting)
        from sculptcut.shorts_layout import SHORTS_PRESETS

        size = (360, 640) if settings.preset in SHORTS_PRESETS else (640, 360)
        scenes = shorts_scenes(tuple(e[2] for e in self.entries[index:end]),
                               self.project, size, self.plan.output_fps)
        self.preparer.request(scenes, self.plan.output_fps, size, audio)

    def prepared(self, clip: ContextClip) -> None:
        if not self.enabled:
            return
        self.prefetching = False
        if self.waiting:
            self.install(self.requested, clip)
        elif self.cached is not None:
            self.reserve.append((self.requested, clip))
        else:
            self.cached = self.requested, clip
            self.warm(clip)
        self.prefetch()

    def warm(self, clip: ContextClip) -> None:
        self.standby.player.setPlaybackRate(1)
        self.standby.limit_frames = True
        self.standby.select(clip.path, 0, clip.spans[-1][1], 0)
        self.standby.play()

    def prefetch(self) -> None:
        if (not self.episode_only and not self.prefetching and not self.reserve
                and self.next_index < len(self.entries)):
            self.queue(self.next_index)

    def standby_frame(self) -> None:
        if self.standby.want_play:
            self.standby.pause()
            self.standby.seek(0)

    def install(self, index: int, clip: ContextClip) -> None:
        self.current = index
        self.waiting = False
        self.busy_changed.emit(False)
        local = max(0, self.initial_cursor-self.entries[index][0])
        self.playback.player.setPlaybackRate(1)
        self.playback.limit_frames = True
        if (local == 0 and self.standby.player.source().toLocalFile() == str(clip.path.resolve())
                .replace("\\", "/") and self.standby.pending is None
                and not self.standby.awaiting_seek):
            self.playback.take_preloaded(self.standby)
        else:
            self.playback.select(clip.path, 0, clip.spans[-1][1], local)
        if self.desired:
            self.playback.play()
        self.prefetch()

    def position(self, ms: int) -> None:
        if self.enabled and self.current >= 0:
            self.cursor = ms-self.direct_offset if self.direct else self.entries[self.current][0]+ms
            self.clock_changed.emit(self.cursor)

    def finished(self) -> None:
        if not self.enabled:
            return
        if self.direct:
            self.desired = False
            return
        if self.cached:
            index, clip = self.cached
            self.cached = self.reserve.pop(0) if self.reserve else None
            self.initial_cursor = self.entries[index][0]
            self.install(index, clip)
            if self.cached:
                self.warm(self.cached[1])
        elif not self.episode_only and self.next_index < len(self.entries):
            self.waiting = True
            self.initial_cursor = self.entries[self.requested][0]
            self.busy_changed.emit(True)
        elif self.preparer.worker is not None and self.requested > self.current:
            self.waiting = True
            self.initial_cursor = self.entries[self.requested][0]
            self.busy_changed.emit(True)
        else:
            self.desired = False

    def toggle(self) -> None:
        self.desired = not self.desired
        if not self.desired:
            self.playback.pause()
        elif not self.waiting:
            self.playback.play()

    def seek_running(self, ms: int) -> bool:
        """Seek within the continuous stream without pausing or reopening it."""
        if not self.enabled or not self.desired or not self.direct:
            return False
        ms = max(self.entries[0][0], min(ms, self.entries[-1][1]-1))
        self.cursor = ms
        self.playback.seek(ms+self.direct_offset)
        return True

    def stop(self) -> None:
        self.enabled = self.desired = self.waiting = False
        self.direct = False
        self.cached = None
        self.reserve.clear()
        self.prefetching = False
        self.preparer.cancel()
        self.playback.pause()
        self.busy_changed.emit(False)
        self.standby.pause()

    def error(self, message: str) -> None:
        self.stop()
        self.failed.emit(message)

    def shutdown(self) -> None:
        self.stop()
        self.standby.shutdown()
        self.preparer.shutdown()
