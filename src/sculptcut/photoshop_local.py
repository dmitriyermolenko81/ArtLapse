"""Frame each ordinary drawing shot locally, without a shared historical canvas."""

from collections.abc import Callable
from math import ceil
from pathlib import Path
from threading import Event

import cv2
import numpy as np
from numpy.typing import NDArray

from sculptcut.domain import CameraPoint, Project, Roi
from sculptcut.drawing_page import SETTINGS as PAGE
from sculptcut.drawing_page import enclosed_page, lock_page_points, page_colour
from sculptcut.drawing_reference import DrawingReference, locked_reference_points
from sculptcut.media import Cancelled, MediaError
from sculptcut.planning import RenderDecision
from sculptcut.subject_camera import still_frame
from sculptcut.webcam import AnalysisCapture, webcam_roi

Reference = tuple[float, int, Roi, Roi]


def local_shots(
    project: Project, decisions: list[RenderDecision],
    clips: dict[int, tuple[Path, list[int], Roi]], references: dict[int, Reference],
    pages: dict[int, list[Roi | None]], canvases: dict[int, list[Roi | None]],
    cancel: Event | None, progress: Callable[[float], None] | None,
    *, prefer_pages: bool = False,
) -> list[RenderDecision]:
    from sculptcut.photoshop import centered_camera, drawing_and_canvas_box

    result = list(decisions)
    sources = {s.id:s for s in project.sources}
    for number, (index, (path, times, viewport)) in enumerate(clips.items()):
        if cancel and cancel.is_set():
            raise Cancelled('Photoshop local framing cancelled')
        d = decisions[index]
        source = sources[d.source_id]
        sw, sh = source.width, source.height
        if source.rotation % 180 == 90:
            sw, sh = sh, sw
        fallback = d.subject_camera
        assert fallback is not None
        sheet = pages[index]
        use_page = (prefer_pages and d.source_id not in project.rois
                    and sum(p is not None for p in sheet)
                    >= len(sheet)*PAGE.minimum_tracking_fraction)
        points: list[CameraPoint] = []
        views: list[Roi] = []
        reference = None
        camera = fallback
        still: NDArray[np.uint8] | None = None
        if d.action == 'HOLD':
            timestamp = d.source_start_us
            if d.provenance in ('user:final_tail','user:shorts_final'):
                timestamp = max(timestamp,d.source_end_us-ceil(
                    1e6*source.fps_den/max(1,source.fps_num)))
            image_path = still_frame(source,timestamp,path.with_suffix('.hold.png'),
                                     cancel,sample_width=960)
            decoded_still = cv2.imread(str(image_path))
            if decoded_still is None:
                raise MediaError('Cannot decode local Photoshop still')
            still = np.asarray(decoded_still,dtype=np.uint8)
            page = enclosed_page(still,rotated=True)
            box, view = drawing_and_canvas_box(still)
            visible = page or view or viewport
            fallback = centered_camera([(times[0],page if use_page and page else box)],
                1,sw,sh,PAGE.framing_margin if use_page else
                project.settings.shorts_tracking_margin,visible)
            camera = fallback
            sheet = [page]*len(times)
            canvases[index] = [visible]*len(times)
        if still is None and not use_page and index in references:
            _, frame_index, box, view = references[index]
            capture = AnalysisCapture(path,webcam_roi(project.settings,d.source_id))
            try:
                capture.set(cv2.CAP_PROP_POS_FRAMES,frame_index)
                ok, decoded_reference = capture.read()
                if not ok:
                    raise MediaError('Cannot decode local Photoshop reference')
                image = np.asarray(decoded_reference,dtype=np.uint8)
            finally:
                capture.release()
            camera = centered_camera([(times[0],box)],1,sw,sh,
                                     project.settings.shorts_tracking_margin,view)
            reference = DrawingReference(image,view,camera)
            camera = reference.camera
        reader = AnalysisCapture(path,webcam_roi(project.settings,d.source_id))
        background = None
        reliable_pages = [j for j,p in enumerate(sheet) if p is not None]
        try:
            for j,t in enumerate(times):
                if cancel and cancel.is_set():
                    raise Cancelled('Photoshop local framing cancelled')
                ok, decoded = (True,still) if still is not None else reader.read()
                if not ok:
                    raise MediaError('Cannot decode local Photoshop tracking frame')
                frame = np.asarray(decoded,dtype=np.uint8)
                page = (sheet[j] or sheet[min(reliable_pages,key=lambda k:abs(k-j))]
                        if use_page and reliable_pages else None)
                visible = page or canvases[index][j] or viewport
                point: CameraPoint | None
                if page is not None:
                    fitted = centered_camera([(t,page)],1,sw,sh,PAGE.framing_margin,page)
                    if background is None and sheet[j] is not None:
                        background = page_colour(frame,page,rotated=True)
                    camera = fitted.model_copy(update={'background_rgb':background})
                    point = fitted.points[0].model_copy(update={
                        'width':fitted.width,'height':fitted.height})
                else:
                    point = reference.point(frame,viewport,t) if reference is not None else None
                    if point is None:
                        point = min(fallback.points,key=lambda p:abs(p.time_us-t)).model_copy(
                            update={'time_us':t})
                        point = point.model_copy(update={
                            'width':point.width or fallback.width,
                            'height':point.height or fallback.height})
                points.append(point.model_copy(update={'visible_region':visible,
                                                        'reference_matched':True}))
                views.append(visible)
        finally:
            reader.release()
        if use_page:
            points = lock_page_points(points)
        else:
            locked = locked_reference_points(list(points),times)
            points = [p.model_copy(update={'visible_region':raw.visible_region,
                                          'reference_matched':True})
                      for p,raw in zip(locked,points,strict=True)]
        left, top = min(v.x for v in views), min(v.y for v in views)
        right = max(v.x+v.width for v in views)
        bottom = max(v.y+v.height for v in views)
        # The decoder sees the union, while the frame-only compositor copies
        # exclusively the visible sheet/canvas at this timestamp.
        camera = camera.model_copy(update={'viewport':Roi(x=left,y=top,width=right-left,
            height=bottom-top),'points':points,'canvas_registered':True})
        result[index] = d.model_copy(update={'subject_camera':camera})
        if progress:
            progress(.5+.5*(number+1)/max(1,len(clips)))
    return result
