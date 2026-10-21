"""Remove ranges from an existing export without touching its source file."""

import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from threading import Event

from sculptcut.audio import add_music
from sculptcut.domain import Project
from sculptcut.media import Cancelled, discover, probe, run_process
from sculptcut.variants import read_variant


def saved_soundtrack(source: Path) -> tuple[Path, bool, float] | None:
    """Return the preserved soundtrack and playback settings for a rendered variant."""
    state = read_variant(source)
    music = state.get("music")
    if not isinstance(music, dict) or not music.get("path"):
        return None
    path = (source.parent / str(music["path"])).resolve()
    if not path.is_file():
        raise ValueError("Сохранённая музыка этого варианта не найдена.")
    if not state.get("project"):
        raise ValueError("У готового ролика нет сохранённых настроек музыки.")
    project = Project.model_validate(state["project"])
    return path, project.settings.music_loop, project.settings.music_fade_seconds


def retained_ranges(duration: int, cuts: list[tuple[int, int]]) -> list[tuple[int, int]]:
    cursor = 0
    kept = []
    for start, end in sorted(cuts):
        if not 0 <= start < end <= duration:
            raise ValueError("Неверные границы удаляемого участка")
        if start > cursor:
            kept.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        kept.append((cursor, duration))
    if not kept:
        raise ValueError("Нельзя удалить весь ролик")
    return kept


def source_time(ranges: list[tuple[int, int]], edited: int) -> int:
    edited = max(0, edited)
    for start, end in ranges:
        if edited < end - start:
            return start + edited
        edited -= end - start
    return ranges[-1][1] if ranges else 0


def edited_time(ranges: list[tuple[int, int]], source: int) -> int:
    elapsed = 0
    for start, end in ranges:
        if source < start:
            return elapsed
        if source < end:
            return elapsed + source - start
        elapsed += end - start
    return elapsed


def selected_ranges(ranges: list[tuple[int, int]], start: int, end: int) -> list[tuple[int, int]]:
    if not 0 <= start < end <= sum(b - a for a, b in ranges):
        raise ValueError("Выделите новый участок на шкале")
    result = []
    elapsed = 0
    for a, b in ranges:
        left, right = max(start, elapsed), min(end, elapsed + b - a)
        if left < right:
            result.append((a + left - elapsed, a + right - elapsed))
        elapsed += b - a
    return result


def cut_output(
    source: Path,
    cuts: list[tuple[int, int]],
    cancel: Event,
    progress: Callable[[float], None],
) -> Path:
    info = probe(source)
    if info.duration_us is None:
        raise ValueError("Не удалось определить длительность ролика")
    ranges = retained_ranges(info.duration_us, cuts)
    if not cuts:
        raise ValueError("Выберите хотя бы один участок")
    soundtrack = saved_soundtrack(source)
    ffmpeg = discover("ffmpeg")
    with tempfile.TemporaryDirectory(prefix="cut-", dir=source.parent) as directory:
        folder = Path(directory)
        for index, (start, end) in enumerate(ranges):
            arguments = [
                ffmpeg,
                "-v",
                "error",
                "-nostdin",
                "-ss",
                str(start / 1e6),
                "-i",
                str(source),
                "-t",
                str((end - start) / 1e6),
                "-map",
                "0:v:0",
                *(["-an"] if soundtrack else ["-map", "0:a:0?"]),
                "-c:v",
                "libx264",
                "-bf",
                "0",
                "-vf",
                "setpts=PTS-STARTPTS",
                "-preset",
                "fast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-avoid_negative_ts",
                "make_zero",
                str(folder / f"part-{index}.mp4"),
            ]
            run_process(arguments, cancel)
            progress((index + 1) / (len(ranges) + 2 if soundtrack else len(ranges) + 1))
        playlist = folder / "parts.txt"
        playlist.write_text(
            "".join(f"file 'part-{i}.mp4'\n" for i in range(len(ranges))), encoding="utf-8"
        )
        result = folder / "ready.mp4"
        run_process(
            [
                ffmpeg,
                "-v",
                "error",
                "-nostdin",
                "-f",
                "concat",
                "-safe",
                "1",
                "-i",
                str(playlist),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(result),
            ],
            cancel,
        )
        expected = sum(b - a for a, b in ranges)
        ready = result
        if soundtrack:
            music, loop, fade_seconds = soundtrack
            ready = folder / "ready-with-music.mp4"
            add_music(
                result,
                music,
                ready,
                expected / 1_000_000,
                loop,
                fade_seconds,
                cancel,
            )
            progress((len(ranges) + 1) / (len(ranges) + 2))
        output = probe(ready)
        if output.duration_us is None or abs(output.duration_us - expected) > max(
            250_000, len(ranges) * 100_000
        ):
            raise ValueError("Длительность результата не соответствует вырезкам")
        if bool(output.audio_streams) != bool(info.audio_streams):
            raise ValueError("Не удалось сохранить звуковую дорожку")
        run_process(
            [ffmpeg, "-v", "error", "-xerror", "-i", str(ready), "-f", "null", "-"], cancel
        )
        for number in range(1, 100_000):
            if cancel.is_set():
                raise Cancelled("Сохранение отменено")
            destination = source.with_name(f"{source.stem}-edit-{number:03}.mp4")
            try:
                # Same-volume hard link publishes atomically and never overwrites a file.
                os.link(ready, destination)
                from sculptcut.variants import save_variant

                save_variant(destination, parent=source, cuts=cuts, operation="cut")
                progress(1)
                return destination
            except FileExistsError:
                continue
    raise ValueError("Не удалось выбрать имя для копии")
