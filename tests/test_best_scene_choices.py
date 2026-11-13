"""Offer strong changes, not every chronologically available omitted window."""

from test_episode_roll import pool, selected_plan
from test_episode_roll import roll_movie as roll_movie
from test_timeline import make_source

from sculptcut.domain import Project
from sculptcut.episode_roll import CHOICES, EpisodeRoll


def test_old_catalogue_recovers_change_strength_from_exact_window_cache(roll_movie) -> None:
    from threading import Event

    from sculptcut.development import DevelopmentEvidence
    from sculptcut.episode_roll_context import load_roll_context
    from sculptcut.work_episodes import process_score

    root, project, plan, episodes, video = roll_movie
    sid = project.sources[0].id
    process_score(root,project,lambda *args:DevelopmentEvidence(0.97),sid,0,3_000_000)
    before = (root/"analysis.sqlite").read_bytes()
    context = load_roll_context(root,video,[],Event())
    assert context.roll.catalogue[0].process_strength == 0.97
    assert (root/"analysis.sqlite").read_bytes() == before


def test_live_wal_strength_is_read_without_writing_project_files(roll_movie) -> None:
    import json
    import sqlite3
    from threading import Event

    from sculptcut.episode_roll_context import load_roll_context
    from sculptcut.work_episodes import process_cache_key

    root, project, plan, episodes, video = roll_movie
    sid = project.sources[0].id
    connection = sqlite3.connect(root/"analysis.sqlite")
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?,?)", (
            "work_process",f"{sid}:0:3000000", process_cache_key(project,sid,0,3_000_000),
            json.dumps({"score":0.99})))
        connection.commit()
        files = [root/"analysis.sqlite",root/"analysis.sqlite-wal"]
        before = [p.read_bytes() for p in files]
        context = load_roll_context(root,video,[],Event())
        assert context.roll.catalogue[0].process_strength == 0.99
        assert [p.read_bytes() for p in files] == before
    finally:
        connection.close()


def test_best_changes_win_over_importance_and_weak_windows_are_hidden() -> None:
    project = Project(sources=[make_source(0,0,36_000_000)])
    plan, episodes = selected_plan(project)
    strengths = {1:0.05,2:0.9,3:0.8,4:0.2,5:0.7}
    episodes = [e.model_copy(update={"process_strength": strengths.get(i,0.2),
                 "score": 100 if i == 1 else 0.1}) for i,e in enumerate(episodes)]
    roll = EpisodeRoll(project,plan,episodes)
    assert [d.episode_id for d in roll.options(1)] == [episodes[i].id for i in (2,3,4,5)]
    assert roll.choices[1].episode_id == episodes[4].id  # weak baseline is still identifiable
    roll.select(1,next(d for d in roll.options(1) if d.episode_id == episodes[2].id))
    assert roll.plan().actual_frame_count == 135
    roll.undo()
    assert not roll.changed


def test_sparse_strong_selection_has_no_weak_filler_and_removes_overlapping_windows() -> None:
    project = Project(sources=[make_source(0,0,36_000_000)])
    plan, episodes = selected_plan(project)
    episodes = [e.model_copy(update={"process_strength":0.01}) for e in episodes]
    episodes[2] = episodes[2].model_copy(update={"process_strength":0.95})
    duplicate = episodes[2].model_copy(update={"start_us":6_100_000,"end_us":9_100_000,
        "work_start_us":6_100_000,"work_end_us":9_100_000,"process_strength":0.9})
    roll = EpisodeRoll(project,plan,[*episodes,duplicate])
    assert [d.episode_id for d in roll.options(1)] == [episodes[2].id,episodes[4].id]


def test_large_gap_offers_only_the_best_few_and_keeps_chronological_order() -> None:
    project = Project(sources=[make_source(0,0,36_000_000)])
    plan, episodes = selected_plan(project)
    # Remove selected middle scene, so the final slot has many earlier alternatives.
    decisions = [d.model_copy(update={"action":"DROP","output_frame_count":0})
                 if d.episode_id == episodes[4].id else d for d in plan.decisions]
    # Preserve its frames in the last slot; candidate availability is still >=1x.
    decisions = [d.model_copy(update={"output_frame_count":105,"source_end_us":22_000_000})
                 if d.episode_id == episodes[6].id else d for d in decisions]
    decisions = [d.model_copy(update={"source_start_us":22_000_000})
                 if d.source_start_us == 21_000_000 else d for d in decisions]
    plan = plan.model_copy(update={"decisions":decisions})
    long_pool = [e.model_copy(update={"process_strength":0.6+i/40}) for i,e in enumerate(pool())]
    # Last slot's current 3s cannot hold 3.5s, but alternatives can have 4s windows.
    long_pool = [e.model_copy(update={"work_start_us":e.start_us,
        "work_end_us":min(36_000_000,e.start_us+4_000_000)}) for e in long_pool]
    roll = EpisodeRoll(project,plan,long_pool)
    options = roll.options(1)
    assert len(options) <= CHOICES.maximum_alternatives+1
    assert [roll.start(d) for d in options] == sorted(roll.start(d) for d in options)
    assert any(d.episode_id == long_pool[10].id for d in options)
