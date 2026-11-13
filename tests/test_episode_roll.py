"""Roll between neighbouring selections without moving a single output cut."""

import json
from dataclasses import replace
from pathlib import Path
from threading import Event

import cv2
import numpy as np
import pytest
from test_intro import make_image
from test_timeline import make_source

from sculptcut.combined_edits import prepare_combined_edit, render_combined_edit
from sculptcut.domain import Override, Project, ProjectSettings, Roi
from sculptcut.episode_roll import EpisodeRoll, catalogue_episodes, catalogue_snapshot
from sculptcut.intro import output_frame_count
from sculptcut.media import discover, run_process
from sculptcut.planning import RenderDecision, build_plan, validate_plan
from sculptcut.project import create_project, save_project
from sculptcut.rendering import render, verify_output
from sculptcut.variants import cut_history, has_manual_edits, read_variant
from sculptcut.work_episodes import WorkEpisode


def wait_loaded(dialog) -> None:
    from time import monotonic

    from PySide6.QtCore import QEventLoop, QTimer

    deadline = monotonic()+8
    while dialog.loading and monotonic() < deadline:
        loop = QEventLoop()
        QTimer.singleShot(10, loop.quit)
        loop.exec()
    assert not dialog.loading, dialog.loading_label.text()


def pool(sid: str = "0") -> list[WorkEpisode]:
    return [WorkEpisode(source_id=sid, start_us=i*3_000_000, end_us=(i+1)*3_000_000,
        work_start_us=i*3_000_000, work_end_us=(i+1)*3_000_000,
        kind="SCULPT", score=0.8, process_checked=True, process_verified=True,
        process_note="work") for i in range(12)]


def selected_plan(project: Project):
    sid = project.sources[0].id
    episodes = pool(sid)
    chosen = {0: 30, 4: 45, 6: 60}
    decisions = [RenderDecision(source_id=sid, source_start_us=e.start_us,
        source_end_us=e.end_us, action="SPEED" if i in chosen else "DROP",
        output_frame_count=chosen.get(i, 0), provenance="story:work_highlight"
        if i in chosen else "story:work_candidate_omission", episode_id=e.id,
        episode_score=e.score) for i, e in enumerate(episodes)]
    plan = build_plan(project, [], 4.5).model_copy(update={"decisions": decisions,
        "actual_frame_count": 135, "target_frame_count": 135})
    validate_plan(project, plan)
    return plan, episodes


@pytest.mark.parametrize("omission", ["story:work_candidate_omission", "story:unverified_work"])
def test_roll_five_between_one_and_seven_keeps_every_cut_and_undo(omission: str) -> None:
    project = Project(sources=[make_source(0, 0, 36_000_000)])
    plan, episodes = selected_plan(project)
    plan = plan.model_copy(update={"decisions": [
        d.model_copy(update={"provenance": omission}) if d.action == "DROP" else d
        for d in plan.decisions]})
    roll = EpisodeRoll(project, plan, episodes)
    assert [d.episode_id for d in roll.options(1)] == [e.id for e in episodes[1:6]]
    assert roll.step(1, 1) and roll.choices[1].episode_id == episodes[5].id
    assert not roll.step(1, 1)  # seven belongs to the following scene
    for expected in (4, 3, 2, 1):
        assert roll.step(1, -1)
        assert roll.choices[1].episode_id == episodes[expected].id
        result = roll.plan()
        assert result.actual_frame_count == plan.actual_frame_count
        assert [d.output_frame_count for d in result.decisions
                if d.output_frame_count] == [30, 45, 60]
        active = [d for d in result.decisions if d.output_frame_count]
        assert active[0] == roll.slots[0].original and active[2] == roll.slots[2].original
    assert not roll.step(1, -1)
    assert roll.options(0)[-1].source_end_us <= roll.choices[1].source_start_us
    while roll.undo_stack:
        roll.undo()
    assert not roll.changed and roll.plan().decisions == plan.decisions
    roll.undo(redo=True)
    assert roll.changed
    with pytest.raises(ValueError, match="соседних"):
        roll.select(1, roll.choices[2])


def test_roll_filters_short_unknown_user_cuts_and_overlay_windows() -> None:
    project = Project(sources=[make_source(0, 0, 36_000_000)], overrides=[
        Override(source_id="0", start_us=3_000_000, end_us=4_000_000, action="DROP")])
    plan, episodes = selected_plan(project)
    episodes[2] = episodes[2].model_copy(update={"process_verified": None})
    episodes[3] = episodes[3].model_copy(update={"work_end_us": 9_100_000})
    decisions = [d.model_copy(update={"provenance": "user:remove_screen"})
                 if d.episode_id == episodes[5].id else d for d in plan.decisions]
    roll = EpisodeRoll(project, plan.model_copy(update={"decisions": decisions}), episodes)
    assert roll.options(1) == [roll.choices[1]]
    assert not roll.step(1, 1) and not roll.changed


def test_roll_can_cross_sources_only_inside_neighbours() -> None:
    a, b = make_source(0, 0, 36_000_000), make_source(1, 36_000_000, 36_000_000)
    project = Project(sources=[a, b])
    left, first = selected_plan(Project(sources=[a]))
    right, second = selected_plan(Project(sources=[b.model_copy(update={"global_start_us": 0})]))
    # Keep A1, A5, B7; A's late and B's early alternatives are in between.
    decisions = [d.model_copy(update={"output_frame_count": 0, "action": "DROP",
                  "provenance": "story:work_candidate_omission"})
                 if d.episode_id == first[6].id else d for d in left.decisions]
    decisions += [d if d.episode_id == second[6].id else d.model_copy(update={
        "output_frame_count": 0, "action": "DROP", "provenance": "story:work_candidate_omission"})
        for d in right.decisions]
    plan = left.model_copy(update={"decisions": decisions,
        "source_fingerprints": {s.id: s.fingerprint for s in project.sources}})
    roll = EpisodeRoll(project, plan, [*first, *second])
    option = next(d for d in roll.options(1) if d.source_id == b.id)
    roll.select(1, option)
    result = roll.plan()
    assert [d.source_id for d in result.decisions if d.output_frame_count] == [a.id, b.id, b.id]
    assert result.actual_frame_count == 135


def test_final_still_and_non_timelapse_are_protected() -> None:
    project = Project(sources=[make_source(0, 0, 36_000_000)])
    plan, episodes = selected_plan(project)
    decisions = [d.model_copy(update={"action": "HOLD", "provenance": "user:final_tail"})
                 if d.episode_id == episodes[6].id else d for d in plan.decisions]
    roll = EpisodeRoll(project, plan.model_copy(update={"decisions": decisions}), episodes)
    assert not roll.step(2, -1) and not roll.step(2, 1)
    assert roll.step(1, 1)
    assert [d for d in roll.plan().decisions if d.output_frame_count][-1] == decisions[6]
    shorts = project.model_copy(update={"settings": ProjectSettings(preset="shorts")})
    with pytest.raises(ValueError, match="таймлапсе"):
        EpisodeRoll(shorts, plan, episodes)


def test_longer_replacement_accelerates_in_the_same_slot() -> None:
    project = Project(sources=[make_source(0, 0, 36_000_000)])
    plan, episodes = selected_plan(project)
    longer = episodes[5].model_copy(update={"start_us": 14_000_000, "end_us": 18_000_000,
        "work_start_us": 14_000_000, "work_end_us": 18_000_000,"process_strength":1.0})
    roll = EpisodeRoll(project, plan, [*episodes, longer])
    choice = next(d for d in roll.options(1) if d.episode_id == longer.id)
    roll.select(1, choice)
    result = roll.plan()
    active = [d for d in result.decisions if d.output_frame_count]
    assert [d.output_frame_count for d in active] == [30, 45, 60]
    assert active[1].source_end_us == active[2].source_start_us
    assert (active[1].source_end_us-active[1].source_start_us) / 1e6 / (45/30) == pytest.approx(8/3)
    assert result.actual_frame_count == plan.actual_frame_count


@pytest.fixture
def roll_movie(tmp_path: Path):
    source = tmp_path / "work.mp4"
    writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*"mp4v"), 10, (320, 180))
    assert writer.isOpened()
    for i in range(360):
        n = i//30
        image = np.full((180, 320, 3), (30+n*12, 50, 160-n*8), np.uint8)
        cv2.putText(image, str(n+1), (110, 110), cv2.FONT_HERSHEY_SIMPLEX, 2, (240,240,240), 3)
        cv2.circle(image, (20+i%280, 150), 10, (20,200,40), -1)
        writer.write(image)
    writer.release()
    music = tmp_path / "music.wav"
    run_process([discover("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                 "sine=frequency=440:duration=7", str(music)])
    cover = tmp_path / "cover.png"
    make_image(cover, (50,80,30))
    root = tmp_path / "project"
    project = create_project(root, [source]).model_copy(update={"settings": ProjectSettings(
        preset="timelapse", target_seconds=5.5, music_path=music, final_view_seconds=0,
        intro_image=cover, intro_seconds=1.5, intro_transition_seconds=0.5)})
    save_project(root, project)
    plan, episodes = selected_plan(project)
    (root / "render_plan.json").write_text(plan.model_dump_json(), encoding="utf-8")
    (root / "work_episodes.json").write_text(json.dumps({
        "source_fingerprints": plan.source_fingerprints, "rois": {},
        "candidates": [e.model_dump() | {"id": e.id} for e in episodes]}), encoding="utf-8")
    video = root / "renders/original.mp4"
    render(root, project, plan, video, encoder="libx264")
    return root, project, plan, episodes, video


def test_real_roll_export_keeps_duration_cover_soundtrack_and_snapshot(roll_movie) -> None:
    root, project, plan, episodes, video = roll_movie
    before = video.read_bytes()
    state = read_variant(video)
    assert len(catalogue_episodes(state["episode_catalogue"], project)) == 12
    edit = prepare_combined_edit(root, video, apply_current=False, exact_variant=True)
    roll = EpisodeRoll(edit.project, edit.plan, episodes)
    assert roll.step(1, 1)
    result = roll.plan()
    assert result.actual_frame_count == plan.actual_frame_count
    output = root / "renders/rolled.mp4"
    render_combined_edit(root, replace(edit, plan=result, base_plan=result), output, Event(),
                         lambda value: None, encoder="libx264", operation="episode_roll")
    expected = output_frame_count(project.settings, 30, 135)
    verify_output(output, expected, 30, expected_audio=True)
    saved = read_variant(output)
    assert saved["operation"] == "episode_roll" and saved["parent"] == video.name
    assert saved["episode_catalogue"] == state["episode_catalogue"]
    assert saved["music"]["tracks"] == state["music"]["tracks"]
    assert cut_history(output) == [] and has_manual_edits(output)
    assert video.read_bytes() == before
    def frame(path, seconds):
        capture = cv2.VideoCapture(str(path))
        capture.set(cv2.CAP_PROP_POS_MSEC, seconds*1000)
        ok, image = capture.read()
        capture.release()
        assert ok
        return image[10:30,10:30].mean(axis=(0,1))

    assert frame(output,2.5)[0] > frame(video,2.5)[0] + 8
    assert np.max(np.abs(frame(output,1.5)-frame(video,1.5))) < 4
    assert np.max(np.abs(frame(output,4.1)-frame(video,4.1))) < 4
    levels = []
    for path in (video, output):
        pcm = path.with_suffix(".f32")
        run_process([discover("ffmpeg"), "-v", "error", "-i", str(path), "-vn",
                     "-ac", "1", "-ar", "48000", "-f", "f32le", str(pcm)])
        samples = np.fromfile(pcm, dtype="<f4")
        levels.append([float(np.sqrt(np.mean(samples[round(t*48000):round(t*48000)+2400]**2)))
                       for t in (2.1, 3.1, 4.1, 5.4)])
    assert np.allclose(levels[0], levels[1], rtol=0.04, atol=0.0001)
    assert all(a>b for a,b in zip(levels[1],levels[1][1:],strict=False))
    assert levels[1][-1] < levels[1][0] * 0.02
    # Reopening the replacement, then an ordinary cut, must start from the
    # replacement, never resurrect the old fifth scene through a neutral layer.
    reopened = prepare_combined_edit(root, output, apply_current=False, exact_variant=True)
    assert [d.episode_id for d in reopened.plan.decisions
            if d.output_frame_count][1] == episodes[5].id
    altered = prepare_combined_edit(root, output, [(0,100_000)], exact_variant=True,
                                    apply_current=False)
    assert any(d.episode_id == episodes[5].id and d.output_frame_count
               for d in altered.plan.decisions)


def test_catalogue_identity_and_saved_copy_survive_new_analysis(roll_movie) -> None:
    root, project, plan, episodes, video = roll_movie
    archived = read_variant(video)["episode_catalogue"]
    changed = project.model_copy(update={"rois": {project.sources[0].id: Roi(
        x=0.1,y=0.1,width=0.8,height=0.8)}})
    assert catalogue_episodes(archived, changed) == []
    assert catalogue_snapshot(root, changed, plan) is None
    (root / "work_episodes.json").write_text('{"candidates":[]}', encoding="utf-8")
    assert len(catalogue_episodes(read_variant(video)["episode_catalogue"], project)) == 12
    (root / "work_episodes.json").write_text('{broken', encoding="utf-8")
    assert catalogue_snapshot(root, project, plan) is None


def test_dialog_roll_playback_cancel_and_cut_clock(roll_movie, tmp_path: Path) -> None:
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication, QWidget

    from sculptcut.episode_roll_dialog import EpisodeRollDialog

    app = QApplication.instance() or QApplication([])
    root, project, plan, episodes, video = roll_movie
    parent = QWidget()
    paths = [root/"project.json", root/"render_plan.json", video]
    before = [p.read_bytes() for p in paths]
    dialog = EpisodeRollDialog(root, video, parent, position=2100,
                                cuts=[(1_000_000,1_200_000)])
    try:
        dialog.show()
        app.processEvents()
        wait_loaded(dialog)

        assert dialog.row == 1
        assert not dialog.export.isEnabled()
        # A pending cut in scene one does not shift the original-file preview
        # of scene two backwards by 0.2 s.
        assert dialog.video_spans[1] == (2000,3500)
        dialog.step(1)
        assert dialog.export.isEnabled() and dialog.roll.choices[1].episode_id == episodes[5].id
        assert dialog.playback.player.playbackRate() == pytest.approx(2)
        dialog.toggle_play()
        loop = QEventLoop()
        QTimer.singleShot(700, loop.quit)
        loop.exec()
        assert not dialog.frame_image.isNull()
        dialog.grab().save(str(tmp_path/"roll-dialog.png"))
        dialog.replay(False)
        assert not dialog.roll.changed
        dialog.replay(True)
        assert dialog.roll.changed
        dialog.reject()
        assert dialog.closed and dialog.images.closed and not dialog.playback.want_play
        assert [p.read_bytes() for p in paths] == before
    finally:
        dialog.shutdown()
        dialog.deleteLater()
        parent.close()
        app.processEvents()


def test_main_window_roll_updates_preview_then_explicit_export(roll_movie, monkeypatch) -> None:
    # Historical experiment integration remains available only to this test.
    monkeypatch.setattr("sculptcut.ui.EPISODE_ROLL_ENABLED", True)
    from time import monotonic

    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    from sculptcut.ui import MainWindow

    app = QApplication.instance() or QApplication([])
    root, project, plan, episodes, video = roll_movie
    before = video.read_bytes()
    window = MainWindow()
    errors = []
    monkeypatch.setattr(window, "show_error", errors.append)

    try:
        window.folder.setText(str(root))
        window.show()
        window.open_video(video, "Готовый таймлапс")
        window.player.pause()
        app.processEvents()
        assert window.roll_button.isEnabled()
        window.roll_button.click()
        dialog = window.roll_dialog
        assert dialog is not None and dialog.isVisible()
        wait_loaded(dialog)
        dialog.select_slot(1)
        dialog.step(1)
        dialog.export.click()
        deadline = monotonic()+25
        while window.job is not None and monotonic() < deadline:
            loop = QEventLoop()
            QTimer.singleShot(50, loop.quit)
            loop.exec()
        assert window.job is None and not errors
        outputs = list((root/"previews").glob("*roll*.mp4"))
        assert len(outputs) == 1
        verify_output(outputs[0], output_frame_count(project.settings,30,135),30,
                      expected_audio=True)
        saved = read_variant(outputs[0])
        assert saved["operation"] == "episode_roll"
        assert [d["episode_id"] for d in saved["plan"]["decisions"]
                if d["output_frame_count"]][1] == episodes[5].id
        assert video.read_bytes() == before
        assert list((root/"renders").glob("*.mp4")) == [video]
        assert saved["plan"]["settings"]["draft"] == 1
        assert window.export_button.isEnabled()
        window.export_button.click()
        deadline = monotonic()+25
        while window.job is not None and monotonic() < deadline:
            loop = QEventLoop()
            QTimer.singleShot(50, loop.quit)
            loop.exec()
        assert window.job is None and not errors
        finals = [p for p in (root/"renders").glob("*.mp4") if p != video]
        assert len(finals) == 1
        exported = read_variant(finals[0])
        assert not exported["plan"]["settings"].get("draft")
        assert [d["episode_id"] for d in exported["plan"]["decisions"]
                if d["output_frame_count"]][1] == episodes[5].id
        verify_output(finals[0], output_frame_count(project.settings,30,135),30,expected_audio=True)
    finally:
        window.close()
        app.processEvents()
