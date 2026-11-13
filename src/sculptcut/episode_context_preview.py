"""Prepare only the auditioned scene window, with bounded decoder memory."""

import hashlib
import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from shutil import rmtree
from tempfile import TemporaryDirectory
from threading import Event

from PySide6.QtCore import QCoreApplication, QObject, QThread, QTimer, Signal

from sculptcut.domain import Project, Roi
from sculptcut.media import Cancelled, discover, run_process
from sculptcut.planning import RenderDecision
from sculptcut.timewarp import pts_expression


@dataclass(frozen=True)
class PreviewScene:
    row: int
    path: Path
    start_ms: int
    end_ms: int
    frames: int
    decision: RenderDecision | None = None
    composition: str = ""


def shorts_scenes(scenes: tuple[PreviewScene, ...], project: Project,
                  size: tuple[int, int], fps: int) -> tuple[PreviewScene, ...]:
    """Match Shorts framing on raw windows; saved video is already composed."""
    from sculptcut.shorts_layout import SHORTS_PRESETS, square_composition, uses_square
    from sculptcut.subject_camera import camera_filter

    if project.settings.preset not in SHORTS_PRESETS:
        return scenes
    width, height = size
    square = uses_square(project.settings)
    result = []
    for scene in scenes:
        d = scene.decision
        if d is None:
            result.append(scene)
            continue
        crop = d.crop
        filters = (f"crop=trunc(iw*{crop.width}/2)*2:trunc(ih*{crop.height}/2)*2:"
                   f"iw*{crop.x}:ih*{crop.y}," if isinstance(crop, Roi) else "")
        if d.subject_camera:
            source = next(s for s in project.sources if s.id == d.source_id)
            sw, sh = source.width, source.height
            if source.rotation % 180 == 90:
                sw, sh = sh, sw
            ratio = d.output_frame_count/fps/((d.source_end_us-d.source_start_us)/1e6)
            filters = camera_filter(d.subject_camera, d.source_start_us, ratio,
                                    sw, sh, width, width if square else height)
        elif square:
            filters += f"scale={width}:{width}:flags=lanczos,"
        else:
            filters += (f"split[back][front];[back]scale={width}:{height}:"
                        f"force_original_aspect_ratio=increase,crop={width}:{height},"
                        "boxblur=30:2[blur];[front]"
                        f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
                        "force_divisible_by=2:flags=lanczos[sharp];"
                        "[blur][sharp]overlay=(W-w)/2:(H-h)/2,")
        if square:
            filters += square_composition(width, height)
        result.append(replace(scene, composition=filters.rstrip(',')))
    return tuple(result)


@dataclass(frozen=True)
class PreviewAudio:
    path: Path
    offset: float
    total_seconds: float
    loop: bool
    fade_in: float
    fade_out: float = 5.0


@dataclass(frozen=True)
class ContextClip:
    path: Path
    spans: tuple[tuple[int, int, int], ...]  # local start/end milliseconds, draft row


def adjoining_scenes(scenes: tuple[PreviewScene, ...]) -> list[PreviewScene]:
    """Read a continuous stretch of a saved video once, keeping original spans."""
    result: list[PreviewScene] = []
    for scene in scenes:
        previous = result[-1] if result else None
        if (previous is not None and previous.decision is None and scene.decision is None
                and previous.path == scene.path and abs(previous.end_ms-scene.start_ms) <= 1
                and abs((previous.frames+scene.frames)*(previous.end_ms-previous.start_ms)
                        /max(1, scene.end_ms-previous.start_ms)-previous.frames) <= 0.5):
            result[-1] = PreviewScene(previous.row, previous.path, previous.start_ms,
                                     scene.end_ms, previous.frames+scene.frames)
        else:
            result.append(scene)
    return result


def prepare_clip(directory: Path, scenes: tuple[PreviewScene, ...], fps: int,
                 size: tuple[int, int], cancel: Event,
                 audio: PreviewAudio | None = None) -> ContextClip:
    identity = [(str(s.path.resolve()), s.path.stat().st_size, s.path.stat().st_mtime_ns,
                 s.start_ms, s.end_ms, s.frames,
                 s.decision.model_dump(mode="json") if s.decision else None,
                 s.composition) for s in scenes]
    audio_key = (None if audio is None else (str(audio.path.resolve()),
        audio.path.stat().st_size, audio.path.stat().st_mtime_ns, audio.offset,
        audio.total_seconds, audio.loop, audio.fade_in, audio.fade_out))
    key = hashlib.sha256(json.dumps([identity, fps, size, audio_key],
                                   sort_keys=True).encode()).hexdigest()
    folder = directory/key
    folder.mkdir(exist_ok=True)
    output = folder/"context.mp4"
    spans = []
    frame = 0
    for scene in scenes:
        spans.append((round(frame*1000/fps), round((frame+scene.frames)*1000/fps), scene.row))
        frame += scene.frames
    result = ContextClip(output, tuple(spans))
    if output.is_file():
        folder.touch()
        return result
    executable = discover("ffmpeg")
    width, height = size
    inputs = adjoining_scenes(scenes)
    try:
        for i, scene in enumerate(inputs):
            duration = (scene.end_ms-scene.start_ms)/1000
            if scene.decision and scene.decision.action == "HOLD":
                duration = max(1.0,duration)
            pts = (pts_expression(scene.decision, fps) if scene.decision else
                   f"PTS*{scene.frames/fps/duration:.12f}")
            filters = ["setpts=PTS-STARTPTS", f"setpts='{pts}'"]
            if scene.decision and scene.decision.action == "HOLD":
                filters = ["trim=end_frame=1", "setpts=PTS-STARTPTS",
                           f"tpad=stop_mode=clone:stop_duration={scene.frames/fps+1:.9f}"]
            crop = scene.decision.crop if scene.decision else None
            if scene.composition:
                filters.append(scene.composition)
            elif isinstance(crop, Roi):
                filters.append(f"crop=trunc(iw*{crop.width}/2)*2:"
                               f"trunc(ih*{crop.height}/2)*2:iw*{crop.x}:ih*{crop.y}")
            filters.extend([f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
                            "force_divisible_by=2",
                            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2", "setsar=1",
                            f"fps={fps}", "tpad=stop_mode=clone:stop_duration=1"])
            run_process([executable, "-v", "error", "-nostdin", "-y", "-threads", "1",
                "-ss", str(scene.start_ms/1000), "-t", str(duration), "-i", str(scene.path),
                "-an", "-sn", "-vf", ",".join(filters), "-filter_threads", "1",
                "-frames:v", str(scene.frames), "-c:v", "libx264", "-preset", "ultrafast",
                "-crf", "23", "-pix_fmt", "yuv420p", "-threads", "1",
                str(folder/f"{i}.mp4")], cancel)
        listing = folder/"clips.txt"
        listing.write_text("".join(f"file '{i}.mp4'\n" for i in range(len(inputs))),
                           encoding="utf-8")
        run_process([executable, "-v", "error", "-nostdin", "-y", "-f", "concat", "-safe", "0",
                     "-i", str(listing), "-c", "copy", "-movflags", "+faststart",
                     str(folder/"pending.mp4")], cancel)
        if audio is not None:
            info = json.loads(run_process([discover("ffprobe"),"-v","error",
                "-show_streams","-show_format","-of","json",str(audio.path)],cancel))
            song = float(info.get("format",{}).get("duration",0))
            offset = audio.offset % song if audio.loop and song else audio.offset
            duration = frame/fps
            if any(s.get("codec_type")=="audio" for s in info.get("streams",[])) and (
                    audio.loop or offset < song):
                fade_in = min(audio.fade_in,audio.total_seconds/2)
                fade_out = min(audio.fade_out,audio.total_seconds)
                envelope = f"min(1,(t+{audio.offset:.9f})/{fade_in:.9f})" if fade_in else "1"
                if fade_out:
                    envelope += ("*pow(sin(PI/2*max(0,min(1,"
                                 f"({audio.total_seconds:.9f}-(t+{audio.offset:.9f}))"
                                 f"/{fade_out:.9f}))),2)")
                loop_args = ["-stream_loop", "-1"] if audio.loop else []
                run_process([executable,"-v","error","-nostdin","-y",
                    "-i",str(folder/"pending.mp4"),*loop_args,"-ss",f"{offset:.9f}",
                    "-i",str(audio.path),"-map","0:v:0","-map","1:a:0","-c:v","copy",
                    "-af",f"asetpts=PTS-STARTPTS,apad,atrim=duration={duration:.9f},"
                    f"volume='{envelope}':eval=frame","-c:a","aac","-b:a","160k",
                    "-t",f"{duration:.9f}","-movflags","+faststart",
                    str(folder/"audio.mp4")],cancel)
                (folder/"pending.mp4").unlink()
                (folder/"audio.mp4").replace(folder/"pending.mp4")
        (folder/"pending.mp4").replace(output)
        for i in range(len(inputs)):
            (folder/f"{i}.mp4").unlink()
        listing.unlink()
        return result
    except BaseException:
        rmtree(folder)
        raise


class ContextWorker(QThread):
    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, directory: Path, scenes: tuple[PreviewScene, ...], fps: int,
                 size: tuple[int, int], audio: PreviewAudio | None = None) -> None:
        super().__init__()
        self.directory, self.scenes, self.fps, self.size = directory, scenes, fps, size
        self.audio = audio
        self.cancel = Event()

    def run(self) -> None:
        try:
            clip = prepare_clip(self.directory, self.scenes, self.fps, self.size, self.cancel,
                                self.audio)
            if not self.cancel.is_set():
                self.ready.emit(clip)
        except Cancelled:
            pass
        except (OSError, ValueError, RuntimeError) as exc:
            if not self.cancel.is_set():
                self.failed.emit(str(exc))


_preparers: set["ContextPreparer"] = set()


class ContextPreparer(QObject):
    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, parent: QObject) -> None:
        super().__init__()  # Outlives a deleted dialog until the cancelled worker finishes.
        self.temporary = TemporaryDirectory(prefix="artlapse-context-")
        self.worker: ContextWorker | None = None
        self.pending: tuple[tuple[PreviewScene, ...], int, tuple[int, int],
                            PreviewAudio | None] | None = None
        self.closed = False
        self.cleaned = False
        self.cleanup_attempts = 0
        _preparers.add(self)
        application = QCoreApplication.instance()
        if application:
            application.aboutToQuit.connect(self.wait_shutdown)

    def request(self, scenes: tuple[PreviewScene, ...], fps: int, size: tuple[int, int],
                audio: PreviewAudio | None = None) -> None:
        self.pending = scenes, fps, size, audio
        if self.worker:
            self.worker.cancel.set()
        else:
            self.start_next()

    def start_next(self) -> None:
        if self.closed or self.pending is None:
            return
        scenes, fps, size, audio = self.pending
        self.pending = None
        directory = Path(self.temporary.name)
        # Session-local LRU, at most eight prepared windows; no project cache changes.
        folders = sorted(directory.iterdir(), key=lambda p: p.stat().st_mtime_ns)
        for folder in folders[:-7]:
            try:
                if folder.resolve().is_relative_to(directory.resolve()):
                    rmtree(folder)
            except PermissionError:
                logging.getLogger("artlapse").debug("Preview still held by decoder: %s", folder)
        worker = ContextWorker(directory, scenes, fps, size, audio)
        self.worker = worker
        worker.ready.connect(lambda clip: self.ready.emit(clip) if not self.closed
                             and self.pending is None and not worker.cancel.is_set() else None)
        worker.failed.connect(lambda error: self.failed.emit(error) if not self.closed
                              and self.pending is None and not worker.cancel.is_set() else None)
        worker.finished.connect(self.finished)
        worker.start()

    def cancel(self) -> None:
        self.pending = None
        if self.worker:
            self.worker.cancel.set()

    def finished(self) -> None:
        if self.worker:
            self.worker.deleteLater()
        self.worker = None
        if self.closed:
            self.cleanup()
        else:
            self.start_next()

    def shutdown(self) -> None:
        self.closed = True
        self.cancel()
        if self.worker is None:
            self.cleanup()

    def wait_shutdown(self) -> None:
        self.shutdown()
        if self.worker and not self.worker.wait(3000):
            logging.getLogger("artlapse").warning("Preview cancellation is still pending")

    def cleanup(self) -> None:
        if self.cleaned:
            return
        try:
            self.temporary.cleanup()
        except OSError as exc:
            if not isinstance(exc, PermissionError) and getattr(exc, "winerror", None) != 267:
                raise
            # QMediaPlayer releases its previous file asynchronously on Windows.
            self.cleanup_attempts += 1
            if self.cleanup_attempts <= 30:
                QTimer.singleShot(100, self.cleanup)
                return
            logging.getLogger("artlapse").warning("Could not remove temporary scene preview",
                                                  exc_info=True)
        self.cleaned = True
        application = QCoreApplication.instance()
        if application:
            application.aboutToQuit.disconnect(self.wait_shutdown)
        _preparers.discard(self)
