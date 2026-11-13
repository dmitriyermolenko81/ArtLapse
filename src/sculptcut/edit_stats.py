"""Source-time removal statistics; acceleration is not counted as deletion."""

from sculptcut.i18n import tr
from sculptcut.planning import RenderPlan
from sculptcut.time_format import format_time


def _episode_count(plan: RenderPlan) -> int:
    # Match the editable blocks and their numbering on the selected version's track.
    return sum(bool(d.output_frame_count) for d in plan.decisions)


def statistics_text(plan: RenderPlan, *, output_frames: int | None = None) -> str:
    total = sum(d.source_end_us - d.source_start_us for d in plan.decisions)
    removed = sum(d.source_end_us - d.source_start_us for d in plan.decisions if d.action == "DROP")

    def duration(us: int) -> str:
        return format_time(us / 1_000_000)

    percent = removed / total * 100 if total else 0
    if output_frames is None:
        output_frames = int(plan.settings.get("actual_output_frames", plan.actual_frame_count))
    text = tr('Монтаж · Исходники: {0} · Вырезано: {1} ({2}%) · Оставлено: {3} · '
              'Ролик: {4} · Эпизодов: {5}').format(duration(total), duration(removed),
        f'{percent:.1f}', duration(total-removed),
        duration(round(output_frames/plan.output_fps*1e6)), _episode_count(plan))
    if any(d.action == "MONTAGE" for d in plan.decisions):
        text += tr(" · В оставшихся диапазонах есть выборочная нарезка кадров.")
    return text
