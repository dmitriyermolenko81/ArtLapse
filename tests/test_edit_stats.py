from sculptcut.edit_stats import statistics_text
from sculptcut.planning import RenderDecision, RenderPlan


def _plan(
    decisions: list[RenderDecision], *, preset: str = "timelapse", **settings: str | int
) -> RenderPlan:
    return RenderPlan(
        source_fingerprints={},
        settings={"preset": preset, **settings},
        output_fps=30,
        target_frame_count=120,
        decisions=decisions,
        actual_frame_count=120,
    )


def _decision(
    start: int, end: int, action: str, provenance: str, source_id: str = "source"
) -> RenderDecision:
    return RenderDecision(
        source_id=source_id,
        source_start_us=start,
        source_end_us=end,
        action=action,  # type: ignore[arg-type]
        output_frame_count=0 if action == "DROP" else 1,
        provenance=provenance,
    )


def test_statistics_show_timelapse_episode_count_for_selected_duration() -> None:
    plan = _plan(
        [
            _decision(0, 5_000_000, "SPEED", "story:static_episode"),
            _decision(5_000_000, 7_000_000, "DROP", "story:episode_omission"),
            _decision(7_000_000, 9_000_000, "SPEED", "story:static_episode"),
        ],
        story="static_episodes",
        episode_count=99,
    )
    assert "Эпизодов: 2" in statistics_text(plan)


def test_statistics_count_retained_ranges_and_merge_adjacent_fragments() -> None:
    plan = _plan(
        [
            _decision(0, 5_000_000, "SPEED", "one"),
            _decision(5_000_000, 8_000_000, "KEEP", "same episode"),
            _decision(8_000_000, 10_000_000, "DROP", "pause"),
            _decision(10_000_000, 12_000_000, "SPEED", "another"),
        ]
    )
    assert "Эпизодов: 2" in statistics_text(plan)


def test_statistics_count_music_stills_as_distinct_episodes() -> None:
    plan = _plan(
        [
            _decision(0, 1_000_000, "DROP", "auto:beat_omission"),
            _decision(1_000_000, 1_100_000, "HOLD", "auto:beat_still"),
            _decision(1_100_000, 1_200_000, "HOLD", "auto:beat_still"),
        ],
        preset="beat_shorts",
    )
    assert "Эпизодов: 2" in statistics_text(plan)


def test_statistics_count_short_highlights_and_final_episode() -> None:
    plan = _plan(
        [
            _decision(0, 1_000_000, "DROP", "user:shorts_summary_omission"),
            _decision(1_000_000, 2_000_000, "SPEED", "user:shorts_highlight"),
            _decision(2_000_000, 3_000_000, "SPEED", "user:shorts_final"),
        ],
        preset="shorts",
    )
    assert "Эпизодов: 2" in statistics_text(plan)


def test_statistics_count_process_shorts_episodes_after_crop_splitting() -> None:
    plan = _plan(
        [
            _decision(0, 1_000_000, "DROP", "user:process_omission"),
            _decision(1_000_000, 2_000_000, "SPEED", "user:process_story"),
            _decision(2_000_000, 3_000_000, "SPEED", "user:process_story"),
            _decision(3_000_000, 4_000_000, "DROP", "user:process_omission"),
            _decision(4_000_000, 5_000_000, "SPEED", "user:process_story"),
            _decision(5_000_000, 6_000_000, "SPEED", "user:shorts_final"),
        ],
        preset="shorts",
    )
    assert "Эпизодов: 3" in statistics_text(plan)
