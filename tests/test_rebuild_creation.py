"""Musical rebuild and Create consume the same form and source-time exclusions."""

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication
from test_timeline import make_source

from sculptcut.combined_edits import CombinedEdit, creation_selection_project
from sculptcut.domain import Override, Project
from sculptcut.planning import RenderDecision, build_plan
from sculptcut.shorts_layout import MUSIC_SHORTS_PRESETS
from sculptcut.ui import MainWindow


def test_selection_merges_output_cuts_and_episode_drops_before_candidate_selection() -> None:
    project = Project(sources=[make_source(0, 0, 12_000_000)], overrides=[
        Override(source_id="0", start_us=0, end_us=3_000_000, action="SET_SPEED", speed=4,
                 restore_omitted=True),
        Override(source_id="0", start_us=3_000_000, end_us=5_000_000, action="DROP"),
        Override(source_id="0", start_us=5_000_000, end_us=12_000_000, action="KEEP"),
    ])
    plan = build_plan(project, [], 4)
    previous = plan.model_copy(update={"decisions": [RenderDecision(
        source_id="0", source_start_us=2_000_000, source_end_us=4_000_000,
        action="DROP", output_frame_count=0, provenance="user:shorts_manual_cut",
    )]})
    edit = CombinedEdit(project, project, plan, previous, Path("old.mp4"), [], [], [])
    before = project.model_dump_json(), previous.model_dump_json()
    selection = creation_selection_project(project, edit)
    assert [(o.action, o.start_us, o.end_us) for o in selection.overrides] == [
        ("DROP", 2_000_000, 5_000_000), ("SET_SPEED", 0, 2_000_000),
        ("KEEP", 5_000_000, 12_000_000),
    ]
    assert selection.overrides[1].restore_omitted
    assert (project.model_dump_json(), previous.model_dump_json()) == before
    assert creation_selection_project(project, None) is project


@pytest.mark.parametrize("preset", sorted(MUSIC_SHORTS_PRESETS))
def test_music_button_routes_current_form_and_pending_edits_through_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preset: str,
) -> None:
    app = QApplication.instance() or QApplication([])
    renders = tmp_path / "renders"
    renders.mkdir()
    selected = renders / "edited.mp4"
    selected.write_bytes(b"Only presence matters; this route test never decodes video")
    music = tmp_path / "music"
    music.mkdir()
    window = MainWindow()
    try:
        window.show()
        window.folder.setText(str(tmp_path))
        window.preset.setCurrentIndex(window.preset.findData(preset))
        window.target.setValue(17)
        window.shorts_layout.setCurrentIndex(window.shorts_layout.findData("square"))
        window.music.setText(str(music))
        window.output_name.setText("Rebuilt")
        window.output_editor.video = selected
        window.output_editor.cuts = [(1_000_000, 2_000_000)]
        monkeypatch.setattr("sculptcut.ui.secrets.randbits", lambda _: 7252)
        calls = []
        monkeypatch.setattr(window, "start_action", lambda action: calls.append((
            action, window.target.value(), window.shorts_layout.currentData(),
            window.output_name.text(), window.music_seed, list(window.output_editor.cuts),
        )))
        window.change_music_button.click()
        window.create_button.click()
        assert calls == [
            ("create", 17, "square", "Rebuilt", 7252, [(1_000_000, 2_000_000)]),
            ("create", 17, "square", "Rebuilt", 7252, [(1_000_000, 2_000_000)]),
        ]
        assert not window.job
    finally:
        window.close()
        app.processEvents()


def test_real_music_rebuild_and_create_use_form_settings_and_keep_all_exclusions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from threading import Event

    from PySide6.QtTest import QTest
    from test_beat_shorts import pulse_track
    from test_creation_edits import wait_creation
    from test_intro import make_image

    from sculptcut.domain import ProjectSettings, Roi
    from sculptcut.fixtures import generate_fixture
    from sculptcut.media import discover, run_process
    from sculptcut.planning import RenderPlan
    from sculptcut.project import create_project, save_project
    from sculptcut.rendering import render, verify_output
    from sculptcut.variants import read_variant

    sample = generate_fixture(tmp_path / "sample.mp4", "local", duration=3, fps=10)
    source = tmp_path / "source.mp4"
    run_process([discover("ffmpeg"), "-v", "error", "-stream_loop", "11", "-i", str(sample),
                 "-c", "copy", str(source)])
    old_music, new_music = tmp_path / "old.wav", tmp_path / "new.wav"
    pulse_track(old_music, [.3 + i*.5 for i in range(30)], seconds=16)
    pulse_track(new_music, [.2 + i*.65 for i in range(24)], seconds=16)
    root = tmp_path / "project"
    project = create_project(root, [source])
    project = project.model_copy(update={"settings": ProjectSettings(
        preset="music_shorts", target_seconds=6, music_path=old_music,
        shorts_layout="square", proxy_width=192, proxy_fps=5, final_view_seconds=0,
    )})
    save_project(root, project)
    base = build_plan(project, [], 6)
    base = base.model_copy(update={
        "settings": base.settings | {"width":160, "height":160},
        "decisions": [d.model_copy(update={"crop":Roi(x=.2, y=0, width=.6, height=1)})
                      for d in base.decisions],
    })
    parent = root / "renders/parent.mp4"
    render(root, project, base, parent, Event(), encoder="libx264")
    changed = project.model_copy(update={"overrides": [Override(
        source_id=project.sources[0].id, start_us=10_000_000, end_us=12_000_000, action="DROP",
    )]})
    save_project(root, changed)
    cover = tmp_path / "cover.png"
    make_image(cover, (0, 220, 0))
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.show()
        window.open_project_folder(root)
        window.preset.setCurrentIndex(window.preset.findData("music_shorts"))
        window.target.setValue(8)
        window.shorts_layout.setCurrentIndex(window.shorts_layout.findData("auto"))
        window.music.setText(str(new_music))
        window.intro_image.setText(str(cover))
        window.intro_seconds = 1.0
        window.output_name.setText("Rebuilt")
        window.open_video(parent, "Parent")
        for _ in range(150):
            if window.player.duration() == 6000:
                break
            QTest.qWait(20)
        window.output_editor.cuts = [(0, 500_000)]  # Old output 0–0.5s = source 0–3s.
        monkeypatch.setattr("sculptcut.ui.secrets.randbits", lambda _: 7252)
        plans = []
        for button in (window.change_music_button, window.create_button):
            window.music_seed = 7252
            button.click()
            assert window.job is not None
            wait_creation(window)
            output = window.last_result_video
            state = read_variant(output)
            result = RenderPlan.model_validate(state["plan"])
            settings = state["project"]["settings"]
            assert settings["target_seconds"] == 8 and settings["shorts_layout"] == "auto"
            assert settings["intro_image"] == str(cover)
            assert settings["intro_seconds"] == 1.0
            assert settings["music_path"] == str(new_music)
            assert (result.settings["width"], result.settings["height"]) == (1080, 1920)
            active = [d for d in result.decisions if d.output_frame_count]
            assert len(active) >= 3
            assert all(d.source_start_us >= 3_000_000 for d in active)
            assert all(d.source_end_us <= 10_000_000 or d.source_start_us >= 12_000_000
                       for d in active)
            assert min(d.output_frame_count for d in active) >= 12
            assert result.settings["beat_sync_aligned_cuts"] == len(active) - 1
            verify_output(output, 240, 30, expected_audio=True)
            assert not window.output_editor.cuts
            plans.append(result)
        assert plans[0].decisions == plans[1].decisions
    finally:
        if window.job is not None:
            window.job.cancel.set()
            window.job.wait(30_000)
        window.close()
        app.processEvents()
