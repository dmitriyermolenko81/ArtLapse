"""Source-time removal statistics; acceleration is not counted as deletion."""

from sculptcut.i18n import tr
from sculptcut.planning import RenderPlan
from sculptcut.shorts_layout import SHORTS_PRESETS
from sculptcut.time_format import format_time


def _episode_count(plan: RenderPlan) -> int:
    preset = plan.settings.get("preset")
    if (
        preset in ("beat_shorts", "photoshop_beat_shorts")
        and plan.settings.get("beat_still_fallback") != "motion"
    ):
        return sum(decision.provenance == "auto:beat_still" for decision in plan.decisions)
    count = 0
    previous_source = ""
    previous_end = -1
    previous_group = ""
    for decision in plan.decisions:
        if decision.action == "DROP":
            previous_end = -1
            previous_group = ""
            continue
        group = (
            decision.provenance
            if preset in SHORTS_PRESETS
            else "retained"
        )
        if (
            decision.source_id != previous_source
            or decision.source_start_us != previous_end
            or group != previous_group
        ):
            count += 1
        previous_source = decision.source_id
        previous_end = decision.source_end_us
        previous_group = group
    return count


def statistics_text(plan: RenderPlan) -> str:
    total = sum(d.source_end_us - d.source_start_us for d in plan.decisions)
    removed = sum(d.source_end_us - d.source_start_us for d in plan.decisions if d.action == "DROP")

    def duration(us: int) -> str:
        return format_time(us / 1_000_000)

    percent = removed / total * 100 if total else 0
    output_frames = int(plan.settings.get("actual_output_frames", plan.actual_frame_count))
    text = (
        tr(f"Последний расчёт · Исходники: {duration(total)} · "
        f"Вырезано: {duration(removed)} ({percent:.1f}%) · "
        f"Оставлено: {duration(total - removed)} · "
        f"Ролик: {duration(round(output_frames / plan.output_fps * 1_000_000))} · "
        f"Эпизодов: {_episode_count(plan)}")
    )
    if any(d.action == "MONTAGE" for d in plan.decisions):
        text += tr(" · В оставшихся диапазонах есть выборочная нарезка кадров.")
    return text
