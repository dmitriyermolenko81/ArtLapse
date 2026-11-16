import cv2
import numpy as np
import pytest

from sculptcut.domain import CameraPoint, ProjectSettings, Roi
from sculptcut.drawing_page import enclosed_page, lock_page_points, page_colour, page_subject
from sculptcut.media import discover, run_process
from sculptcut.photoshop import reframe_photoshop
from sculptcut.planning import build_plan
from sculptcut.project import create_project, save_project
from sculptcut.rendering import render, verify_output


def test_page_geometry_suppresses_pixel_noise_across_episodes_but_keeps_pan_zoom():
    points = [CameraPoint(time_us=i, x=x, y=.5, width=w, height=w*3.16,
        visible_region=Roi(x=.2, y=.2, width=.3, height=.5), reference_matched=True)
        for i, (x,w) in enumerate([(.5,.333), (.5005,.335), (.4998,.334),
                                   (.6,.333), (.5,.25)])]
    locked = lock_page_points(points)
    assert len({(p.x,p.y,p.width,p.height) for p in locked[:3]}) == 1
    assert locked[3].x == .6 and locked[3].width == locked[0].width
    assert locked[4].width == .25
    assert all(a.visible_region == b.visible_region and a.time_us == b.time_us
               and a.reference_matched == b.reference_matched
               for a,b in zip(points,locked,strict=True))


def test_faint_complete_sketch_inside_white_page_excludes_cursor_and_workspace():
    frame = np.full((360,640,3),40,np.uint8)
    frame[40:330,180:460] = 250
    cv2.rectangle(frame,(285,85),(345,305),(235,235,235),1)
    cv2.circle(frame,(315,70),16,(225,225,225),1)
    cv2.line(frame,(315,86),(315,95),(235,235,235),1)
    cv2.drawMarker(frame,(420,170),(120,120,120),markerSize=7)
    original = frame.copy()
    page = enclosed_page(frame)
    assert page is not None
    box = page_subject(frame,page)
    assert box is not None
    assert abs(box.x*640-283)<4 and abs((box.x+box.width)*640-348)<4
    assert box.height*360 > 245
    assert np.array_equal(frame,original)
    frame[40:330,180:460] = (20,180,240)
    assert page_subject(frame,page) is None  # coloured sheet keeps the page camera
    frame[40:330,180:460] = 250
    cv2.drawMarker(frame,(420,170),(120,120,120),markerSize=7)
    assert page_subject(frame,page) is None


def test_enclosed_coloured_or_white_paper_is_not_the_application_workspace():
    for colour in [(20, 180, 240), (240, 240, 240)]:
        frame = np.full((360, 640, 3), 40, np.uint8)
        frame[80:240, 200:360] = colour
        page = enclosed_page(frame)
        assert page is not None
        assert abs(page.x-200/640) < .005 and abs(page.width-160/640) < .005
    assert enclosed_page(np.full((360, 640, 3), 40, np.uint8)) is None
    # A picture cut by the screen boundary must not masquerade as a complete page.
    frame[:, :300] = (20, 180, 240)
    assert enclosed_page(frame) is None


def test_ordinary_rotated_page_excludes_workspace_without_changing_whole_model_detection():
    frame = np.full((360,640,3),40,np.uint8)
    corners = np.int32(cv2.boxPoints(((320,180),(200,160),23)))
    cv2.fillConvexPoly(frame,corners,(20,180,240))
    assert enclosed_page(frame) is None
    page = enclosed_page(frame,rotated=True)
    assert page is not None
    assert page.width < .5 and page.height < .7
    assert abs(page.x+page.width/2-.5) < .005
    assert page_colour(frame,page,rotated=True) == (240,180,20)
    frame[:]=40
    assert enclosed_page(frame,rotated=True) is None


@pytest.mark.parametrize('keep_model',[False,True])
def test_illustration_fits_one_page_despite_source_pan_zoom_and_weak_subject(
    tmp_path, monkeypatch, keep_model,
):
    paths = []
    for index, (x, y, size) in enumerate([(100, 70, 180), (260, 130, 120), (190, 40, 240)]):
        path = tmp_path / f"source-{index}.mp4"
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*"mp4v"), 10, (640, 360))
        assert writer.isOpened()
        for i in range(10):
            frame = np.full((360, 640, 3), 40, np.uint8)
            cv2.putText(frame, "FILE TOOLS 123", (10, 18), cv2.FONT_HERSHEY_SIMPLEX,
                        .5, (240, 240, 240), 1)
            px, ps = (x+i*2, size+i*4) if index > 0 else (x, size)
            frame[y:y+ps, px:px+ps] = (20, 180, 240)
            if index == 0 and i in (0,5):
                frame[y:y+ps, px:px+ps] = 160  # one genuinely new, unrecognized grey sheet
            cv2.ellipse(frame, (px+ps//2, y+ps//2), (ps//3, ps//5), 0, 15, 170,
                        (25, 170, 225), 2)
            # Quarter/half coordinates stay on the pixel grid at every source zoom.
            cv2.circle(frame, (px+round(ps*.25), y+round(ps*.5)), 2, (20, 20, 20), -1)
            writer.write(frame)
        writer.release()
        paths.append(path)
    root = tmp_path / "project"
    project = create_project(root, paths).model_copy(update={"settings": ProjectSettings(
        preset="photoshop_shorts", photoshop_keep_model=keep_model, target_seconds=3,
        output_fps=10, final_view_seconds=0,
    )})
    save_project(root, project)
    base = build_plan(project, [], 3)
    decisions = list(base.decisions)
    decisions[1] = decisions[1].model_copy(update={
        'action':'HOLD', 'provenance':'user:shorts_final'})
    plan = reframe_photoshop(project, base.model_copy(update={'decisions':decisions}), None)
    assert plan.settings["photoshop_camera"] == (
        "enclosed-page-361" if keep_model else "local-episode-366")
    assert plan.settings["photoshop_registered_shots"] == 3
    if not keep_model:
        background = plan.decisions[0].subject_camera.background_rgb
        assert background is not None and max(background)-min(background) > 100
    assert all(p.visible_region is not None for d in plan.decisions
               for p in d.subject_camera.points)
    assert len({(p.x, p.y, p.width, p.height)
                for p in plan.decisions[1].subject_camera.points}) == 1
    if not keep_model:
        restored = project.model_copy(update={'settings':project.settings.model_copy(
            update={'photoshop_keep_model':True})})
        restored_plan = reframe_photoshop(restored,plan,None)
        assert not restored_plan.settings.get('photoshop_frame_only')
        def unexpected_history(*args,**kwargs):
            pytest.fail('Ordinary Photoshop must not accumulate historical pixels')
        monkeypatch.setattr('sculptcut.canvas_history.CanvasMemory.update',unexpected_history)
    output = render(root, project, plan, tmp_path / "page.mp4", encoder="libx264")
    verify_output(output, 30, 10)
    centers = []
    for index, seconds in enumerate([.5, 1.1, 1.8, 2.5]):
        image = tmp_path / f"frame-{index}.png"
        run_process([discover("ffmpeg"), "-v", "error", "-ss", str(seconds),
                     "-i", str(output), "-frames:v", "1", str(image)])
        frame = cv2.imread(str(image))
        if not keep_model and index == 0:
            assert np.max(np.abs(frame[1230,810].astype(int)-160)) < 10
        dark = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) < 80
        assert float(dark.mean()) < .002  # no grey menus/workspace around the paper
        yy, xx = np.where(dark)
        assert len(xx) > 100
        centers.append((xx.mean(), yy.mean()))
    assert np.max(np.ptp(np.array(centers), axis=0)) < 6
