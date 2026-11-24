"""Check stream-copy remuxes using the MP4 sample table and the newly encoded audio."""

import json
from pathlib import Path
from threading import Event

from sculptcut.media import MediaError, discover, run_process


def video_samples(video: Path, cancel: Event | None) -> int:
    data = run_process([discover("ffprobe"), "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=nb_frames", "-of", "json", str(video)], cancel)
    try:
        return int(json.loads(data)["streams"][0]["nb_frames"])
    except (ValueError, IndexError, KeyError, TypeError) as exc:
        raise MediaError("Не удалось проверить число кадров MP4") from exc


def verify_remux(source: Path, output: Path, cancel: Event | None) -> None:
    if video_samples(source, cancel) != video_samples(output, cancel):
        raise MediaError("При записи звука изменилось число кадров")
    run_process([discover("ffmpeg"), "-v", "error", "-xerror", "-i", str(output),
                 "-map", "0:a:0", "-f", "null", "-"], cancel)


def decode_delay(source: Path, fps: int, cancel: Event | None) -> int:
    first_dts = run_process([discover("ffprobe"), "-v", "error", "-select_streams", "v:0",
                             "-read_intervals", "%+#1", "-show_entries", "packet=dts_time",
                             "-of", "csv=p=0", str(source)], cancel).strip()
    return max(0, -round(float(first_dts) * fps))
