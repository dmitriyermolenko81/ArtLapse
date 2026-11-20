import cv2
import numpy as np

from sculptcut.canvas_edges import dark_document_bounds, trim_horizontal_chrome
from sculptcut.canvas_history import CanvasMemory
from sculptcut.photoshop import drawing_and_canvas_box


def test_dark_photoshop_workspace_excludes_bars_and_margins():
    frame = np.full((540, 960, 3), 38, np.uint8)
    frame[:34] = 65
    frame[516:522] = 73
    frame[522:] = 2
    frame[34:516, :15] = 81
    frame[34:516, 954:] = 73
    frame[34:460, 224:726] = (68, 50, 39)
    frame[350:460, 224:726] = (110, 87, 72)
    frame[105:405, 404:555] = (21, 105, 213)
    assert dark_document_bounds(frame) == (224, 35, 726, 460)

    frame[485:487, 175:805] = (174, 157, 126)
    assert dark_document_bounds(frame) == (224, 35, 726, 460)

    # A far zoom-out must evaluate colour on the page, even if workspace
    # takes up most of the pixels inside the application window.
    frame[34:516, 15:954] = 38
    frame[180:431, 376:544] = (68, 50, 39)
    assert dark_document_bounds(frame) == (376, 180, 544, 431)

    # A zoomed-in page can extend all the way to the toolbars.
    frame[34:516, 15:954] = (68, 50, 39)
    assert dark_document_bounds(frame) == (16, 35, 953, 515)
    assert dark_document_bounds(frame[34:516, 15:954]) is None


def test_window_bands_are_excluded_before_the_canvas_remembers_them():
    frame = np.full((600, 400, 3), (210, 190, 170), np.uint8)
    frame[8:16] = 25
    frame[575:584] = 25
    frame[565:567, 60:340] = 80  # scrollbar above the bottom border
    cv2.rectangle(frame, (150, 50), (250, 550), (20, 20, 20), -1)
    bounds = trim_horizontal_chrome(frame, (0, 0, 400, 600))
    assert bounds[1] > 16 and bounds[3] < 565
    memory = CanvasMemory()
    result = memory.update(frame, (0, 0, 400, 600), (210, 190, 170))
    assert np.all(result[8:16] == (210, 190, 170))
    assert np.all(result[565:584] == (210, 190, 170))
    assert np.array_equal(result[50:550, 150:250], frame[50:550, 150:250])
    assert not np.any(memory.known[:bounds[1]])


def test_artwork_and_middle_horizontal_lines_are_preserved():
    frame = np.full((400, 600, 3), 210, np.uint8)
    frame[6:30, 200:400] = 20  # head near the top
    frame[370:395, 180:420] = 20  # feet near the bottom
    frame[200:204] = 20  # a genuine line inside the drawing
    bounds = (0, 0, 600, 400)
    assert trim_horizontal_chrome(frame, bounds) == bounds
    memory = CanvasMemory()
    assert np.array_equal(memory.update(frame, bounds), frame)


def test_unseen_margins_continue_the_canvas_colour_without_a_separate_band():
    frame = np.empty((400, 200, 3), np.uint8)
    frame[:] = np.arange(100, 200, .25, dtype=np.float32).astype(np.uint8)[:, None, None]
    cv2.rectangle(frame, (80, 80), (120, 320), (20, 20, 20), -1)
    memory = CanvasMemory()
    result = memory.update(frame, (0, 30, 200, 370), (90, 90, 90))
    assert np.all(result[:30] == frame[30, 0])
    assert np.all(result[370:] == frame[369, 0])
    assert np.array_equal(result[80:320, 80:120], frame[80:320, 80:120])
    detail = np.full_like(frame, 240)
    result = memory.update(detail, (0, 200, 200, 350), (90, 90, 90))
    assert np.all(result[:30] == frame[30, 0])  # a feet patch cannot recolour the top margin
    assert np.all(result[370:] == frame[369, 0])


def test_background_fragments_beyond_a_canvas_border_do_not_expand_the_viewport():
    frame = np.full((400, 600, 3), (210, 190, 170), np.uint8)
    frame[10:16] = 20
    frame[380:386] = 20
    cv2.rectangle(frame, (270, 50), (330, 340), (60, 60, 60), -1)
    box, canvas = drawing_and_canvas_box(frame)
    assert box is not None and canvas is not None
    assert box.y <= 50/400 and box.y+box.height >= 340/400
    assert canvas.y >= 16/400
    assert round((canvas.y+canvas.height)*400) <= 380


def test_zoom_out_workspace_touching_the_crop_edge_is_not_a_thin_toolbar():
    frame = np.full((600, 400, 3), (210, 190, 170), np.uint8)
    frame[:55] = 35  # >6%: workspace exposed by source zoom-out
    frame[535:] = 35
    cv2.rectangle(frame, (150, 90), (250, 500), (20, 20, 20), -1)
    bounds = trim_horizontal_chrome(frame, (0, 0, 400, 600))
    assert bounds[1] > 55 and bounds[3] < 535
    result = CanvasMemory().update(frame, (0, 0, 400, 600), (210, 190, 170))
    assert np.all(result[:55] == (210, 190, 170))
    assert np.all(result[535:] == (210, 190, 170))
    assert np.array_equal(result[90:500, 150:250], frame[90:500, 150:250])


def test_wide_dark_artwork_away_from_the_edge_keeps_the_thickness_guard():
    frame = np.full((600, 400, 3), 210, np.uint8)
    frame[10:60] = 25
    assert trim_horizontal_chrome(frame, (0, 0, 400, 600)) == (0, 0, 400, 600)
def test_faint_guide_cleanup_is_local_and_preserves_gradient_and_figure():
    from sculptcut.canvas_edges import clear_outer_guides

    frame = np.empty((400,300,3),np.uint8)
    for y in range(400):
        frame[y] = (210-y//20,180-y//20,160-y//20)
    frame[70:320,110:190] = (35,45,55)
    clean = frame.copy()
    frame[15:17,40:260] -= 12
    before = frame.copy()
    clear_outer_guides(frame)
    assert np.array_equal(frame[25:],before[25:])
    assert np.array_equal(frame[:12],before[:12])
    assert np.max(np.abs(frame[15:17,40:260].astype(float)-clean[15:17,40:260])) <= 4
