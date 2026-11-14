"""Source-time removal statistics; acceleration is not counted as deletion."""

from sculptcut.i18n import tr
from sculptcut.planning import RenderPlan
from sculptcut.shorts_layout import SHORTS_PRESETS
from sculptcut.time_format import format_time


def _episode_count(plan: RenderPlan) -> int:
    # Match the editable blocks and their numbering on the selected version's track.
    return sum(bool(d.output_frame_count) for d in plan.decisions)


def analyzed_episode_count(plan: RenderPlan) -> int | None:
    key = ('short_candidates_verified' if plan.settings.get('preset') in SHORTS_PRESETS
           else 'work_verified_count')
    value = plan.settings.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def statistics_text(plan: RenderPlan, *, output_frames: int | None = None) -> str:
    total = sum(d.source_end_us - d.source_start_us for d in plan.decisions)
    removed = sum(d.source_end_us - d.source_start_us for d in plan.decisions if d.action == "DROP")

    def duration(us: int) -> str:
        return format_time(us / 1_000_000)

    percent = removed / total * 100 if total else 0
    if output_frames is None:
        output_frames = int(plan.settings.get("actual_output_frames", plan.actual_frame_count))
    analyzed = analyzed_episode_count(plan)
    text = tr('Монтаж · Исходники: {0} · Вырезано: {1} ({2}%) · Оставлено: {3} · '
              'Ролик: {4} · После анализа: {5} · В ролике: {6}').format(
        duration(total), duration(removed),
        f'{percent:.1f}', duration(total-removed),
        duration(round(output_frames/plan.output_fps*1e6)),
        analyzed if analyzed is not None else '—',
        _episode_count(plan))
    if any(d.action == "MONTAGE" for d in plan.decisions):
        text += tr(" · В оставшихся диапазонах есть выборочная нарезка кадров.")
    return text
