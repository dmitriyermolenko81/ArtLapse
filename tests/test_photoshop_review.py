from pathlib import Path

from test_timeline import make_source

from sculptcut.domain import CameraPoint, Project, ProjectSettings, Roi, SubjectCamera
from sculptcut.photoshop_review import alignment_markers
from sculptcut.planning import RenderDecision, RenderPlan
from sculptcut.variants import save_variant


def test_review_offsets_intro_baked_cuts_and_tracking_gaps(tmp_path: Path) -> None:
    source = make_source(0, 0, 9_000_000)
    project = Project(sources=[source], settings=ProjectSettings(
        preset='photoshop_shorts', photoshop_keep_model=True,
        intro_image=tmp_path/'intro.png', intro_seconds=1.5, intro_transition_seconds=.5,
    ))
    roi = Roi(x=.2, y=.2, width=.6, height=.6)
    camera = SubjectCamera(viewport=roi, width=1, height=1, background=100,
        canvas_registered=True, fit_subject=True,
        points=[CameraPoint(time_us=t, x=.5, y=.5, reference_matched=matched)
                for t, matched in ((0, True), (1_000_000, False), (2_000_000, True))])
    tracked = RenderDecision(source_id=source.id, source_start_us=0, source_end_us=3_000_000,
        action='KEEP', output_frame_count=30, provenance='test', subject_camera=camera)
    local = tracked.model_copy(update={'source_start_us': 3_000_000,
        'source_end_us': 6_000_000, 'subject_camera': camera.model_copy(update={
            'canvas_registered': False, 'frame_only': True, 'closeup_group': 1,
            'points': [CameraPoint(time_us=3_000_000, x=.5, y=.5, reference_matched=True,
                                   visible_region=roi)]})})
    hold = tracked.model_copy(update={'source_start_us': 6_000_000, 'source_end_us': 9_000_000,
        'action': 'HOLD', 'output_frame_count': 10, 'subject_camera': None})
    plan = RenderPlan(source_fingerprints={source.id: source.fingerprint}, settings={},
        output_fps=10, target_frame_count=70, actual_frame_count=70,
        decisions=[tracked, local, hold])
    video = tmp_path/'cut.mp4'
    save_variant(video, project=project, plan=plan, operation='cut',
                 cuts=[(1_900_000, 2_100_000)])

    duration, markers = alignment_markers(video, project, plan)
    assert duration == 78
    assert [(m.start, m.end) for m in markers] == [(16, 19), (19, 24)]
    assert [m.original_time(m.start) for m in markers] == [600_000, 1_100_000]
    assert markers[1].original_time(23) == 1_500_000
