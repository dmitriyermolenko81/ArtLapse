"""The final planning pass repairs an undersized automatic finale after budgeting."""
from PySide6.QtWidgets import QApplication
from test_creation_edits import wait_creation
from test_episode_roll import roll_movie as roll_movie

from sculptcut import bookends
from sculptcut.budget import distribute
from sculptcut.planning import RenderPlan
from sculptcut.project import save_project
from sculptcut.rendering import verify_output
from sculptcut.ui import MainWindow
from sculptcut.variants import read_variant


def test_create_rechecks_final_presentation_after_budgeting(roll_movie, monkeypatch):
    root, project, _, _, _ = roll_movie
    project = project.model_copy(update={'settings': project.settings.model_copy(update={
        'target_seconds': 6, 'final_view_seconds': 4, 'expressive_pacing': False,
        'proxy_width': 320, 'proxy_fps': 5})})
    save_project(root, project)
    original = bookends.present_final_result
    calls = []

    def early_shortage(project, plan):
        result = original(project, plan)
        calls.append(result)
        if len(calls) == 1:
            final = next(d for d in reversed(result.decisions)
                         if d.output_frame_count and d.action == 'HOLD')
            bodies = [d for d in result.decisions if d.output_frame_count and d.action == 'SPEED']
            old = [d.output_frame_count for d in bodies]
            caps = [max(n, (d.source_end_us-d.source_start_us)*result.output_fps//1_000_000)
                    for n, d in zip(old, bodies, strict=True)]
            counts = iter(distribute(old, [float(n) for n in old], caps,
                                     sum(old)+final.output_frame_count-1))
            result = result.model_copy(update={'decisions': [d.model_copy(update={
                'output_frame_count': 1 if d is final else next(counts)})
                if d is final or d in bodies else d for d in result.decisions],
                'settings': result.settings | {'final_view_frames': 1}})
        return result

    monkeypatch.setattr(bookends, 'present_final_result', early_shortage)
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    errors = []
    monkeypatch.setattr(window, 'show_error', errors.append)
    try:
        window.show()
        window.open_project_folder(root)
        window.output_name.setText('FinalBudget')
        window.create_button.click()
        wait_creation(window)
        state = read_variant(window.last_result_video)
        plan = RenderPlan.model_validate(state['plan'])
        assert len(calls) >= 2 and not errors
        assert bookends.final_frames(plan) >= 15
        assert plan.actual_frame_count == 150  # Remaining second belongs to the cover.
        verify_output(window.last_result_video, 180, 30, expected_audio=True)
    finally:
        if window.job:
            window.job.cancel.set()
            window.job.wait(30_000)
        window._allow_close = True
        window.close()
        app.processEvents()
