"""Frozen entrypoint and opt-in diagnostic window launch."""

import argparse
import json
import sys
from pathlib import Path
from time import monotonic

from PySide6.QtCore import QCoreApplication, QEvent, QPointF, QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication

from sculptcut.application_identity import configure_application_identity, windows_application_id
from sculptcut.cli import main as cli_main
from sculptcut.logging_config import configure_logging
from sculptcut.ui import EPISODE_ROLL_ENABLED, MainWindow
from sculptcut.ui import main as gui_main


def main() -> int:
    if Path(sys.executable).stem.lower().endswith("-cli"):
        return cli_main()
    if "--smoke-directory" not in sys.argv:
        return gui_main()
    parser = argparse.ArgumentParser(description="Diagnostic window smoke test")
    parser.add_argument("--smoke-directory", type=Path, required=True)
    parser.add_argument("--smoke-video", type=Path)
    parser.add_argument("--smoke-language", choices=("ru", "en"))
    parser.add_argument("--smoke-check-updates", action="store_true")
    parser.add_argument("--smoke-roll-project", type=Path)
    parser.add_argument("--smoke-context", action="store_true")
    parser.add_argument("--smoke-selection-project", type=Path)
    parser.add_argument('--smoke-music-project', type=Path)
    parser.add_argument('--smoke-tracks', action='store_true')
    parser.add_argument('--smoke-hover', action='store_true')
    args = parser.parse_args()
    configure_logging().info("Desktop smoke started", extra={"event": "startup"})
    args.smoke_directory.mkdir(parents=True, exist_ok=True)
    configure_application_identity()
    app = QApplication(sys.argv)
    window = MainWindow()
    window.recent_settings = QSettings(
        str(args.smoke_directory / "settings.ini"), QSettings.Format.IniFormat
    )
    if args.smoke_language:
        window.change_language(args.smoke_language)
    window.show()
    update_results: list[dict[str, object]] = []
    if args.smoke_check_updates:
        window.updates.checker.completed.connect(
            lambda release, error: update_results.append(
                {"error": error, "offered": release is not None},
            ),
        )
        window.updates.schedule_startup()
    if args.smoke_video:
        window.player.setSource(QUrl.fromLocalFile(str(args.smoke_video.resolve())))
        window.player.play()

    roll_dialog = None
    if EPISODE_ROLL_ENABLED and args.smoke_roll_project and args.smoke_video:
        from sculptcut.episode_roll_dialog import EpisodeRollDialog

        window.player.pause()
        window.folder.setText(str(args.smoke_roll_project))
        window.open_video(args.smoke_video, "Готовый таймлапс")
        window.player.pause()
        roll_dialog = EpisodeRollDialog(args.smoke_roll_project, args.smoke_video, window,
                                        volume_state=window.volume_state)
        def loaded_roll() -> None:
            assert roll_dialog is not None
            rows = list(range(len(roll_dialog.roll.slots)))
            if args.smoke_context:
                middle = (len(rows)-1)/2
                rows.sort(key=lambda row: abs(row-middle))
            for row in rows:
                if len(roll_dialog.roll.options(row)) > 1:
                    roll_dialog.select_slot(row)
                    if not roll_dialog.roll.step(row, 1):
                        roll_dialog.roll.step(row, -1)
                    roll_dialog.refresh()
                    break
            if args.smoke_context:
                roll_dialog.toggle_context()
            else:
                roll_dialog.toggle_play()

        roll_dialog.ready.connect(loaded_roll)
        roll_dialog.show()

    selection_dialog = None
    selection_context: list[str] = []
    hover_checked = False
    if args.smoke_selection_project and args.smoke_video:
        from sculptcut.source_selection_dialog import SourceSelectionDialog

        window.folder.setText(str(args.smoke_selection_project))
        window.open_video(args.smoke_video,"Готовый таймлапс")
        window.player.pause()
        window.trim_mode.setCurrentIndex(1)
        candidate = window.selection_dialog
        assert isinstance(candidate,SourceSelectionDialog)
        selection_dialog = candidate

        def loaded_selection() -> None:
            nonlocal hover_checked
            assert selection_dialog is not None
            gap = next(d for d in selection_dialog.session.plan.decisions if d.action=="DROP"
                       and d.source_end_us-d.source_start_us>1_000_000)
            selection_dialog.sources.setCurrentIndex(
                selection_dialog.sources.findData(gap.source_id))
            start = gap.source_start_us+100_000
            selection_dialog.select_range(start,min(gap.source_end_us,start+1_500_000))
            selection_dialog.speed.setValue(2)
            selection_dialog.keep.click()
            if args.smoke_tracks:
                track = selection_dialog.music_track
                assert track is not None and track.clips
                plan = selection_dialog.session.plan
                clip = track.clips[0]
                original_music = list(track.clips)
                track.commit([clip.model_copy(update={
                    'end_us': clip.start_us+1_000_000, 'at_us': 100_000})])
                track.lane.setValue(600)
                if args.smoke_hover:
                    router = selection_dialog.hover_keys
                    assert router is not None
                    window.play_button.setFocus()
                    point = QPointF(track.lane.x_at(600), 20)
                    QApplication.sendEvent(track.lane, QMouseEvent(QEvent.Type.MouseMove,
                        point, QPointF(track.lane.mapToGlobal(point.toPoint())),
                        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier))
                    assert router.target() is track.lane
                    QApplication.sendEvent(window.play_button, QKeyEvent(QEvent.Type.KeyPress,
                        Qt.Key.Key_Q, Qt.KeyboardModifier.NoModifier))
                    assert selection_dialog.session.plan == plan
                    if track.locked:
                        assert track.clips == original_music and not track.changed
                        assert not track.options_button.isEnabled()
                    else:
                        assert len(track.clips) == 2 and track.lane.split_positions
                    # The same key over video must leave the music clips intact.
                    source_lane = router.video_lane
                    row, a, b = source_lane.blocks[-1]
                    at = (a+b)//2
                    point = QPointF(source_lane.x_at(at), 36)
                    QApplication.sendEvent(source_lane, QMouseEvent(QEvent.Type.MouseMove,
                        point, QPointF(source_lane.mapToGlobal(point.toPoint())),
                        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier))
                    assert router.target() is source_lane
                    QApplication.sendEvent(window.play_button, QKeyEvent(QEvent.Type.KeyPress,
                        Qt.Key.Key_Q, Qt.KeyboardModifier.NoModifier))
                    assert len(track.clips) == (1 if track.locked else 2)
                    assert selection_dialog.session.plan != plan
                    hover_checked = True
                else:
                    track.split()
                    assert len(track.clips) == (1 if track.locked else 2)
                    assert selection_dialog.session.plan == plan
                selection_dialog.seek_montage(600)
                selection_dialog.play_montage()
                return
            selection_dialog.preparer.ready.connect(
                lambda clip:selection_context.append(str(clip.path)))
            selection_dialog.context.click()

        selection_dialog.ready.connect(lambda: QTimer.singleShot(100, loaded_selection)
            if args.smoke_hover else loaded_selection())

    music_dialog = None
    if args.smoke_music_project and args.smoke_video:
        from sculptcut.music_edit_dialog import MusicEditDialog

        window.player.pause()
        music_dialog = MusicEditDialog(args.smoke_music_project, args.smoke_video, window)
        music_dialog.show()
        music_dialog.preview()
    deadline = monotonic() + 12

    def finish() -> None:
        if music_dialog is not None and (music_dialog.worker is not None
                or music_dialog.frame_image.isNull()) and monotonic() < deadline:
            QTimer.singleShot(100, finish)
            return
        track = selection_dialog.music_track if selection_dialog is not None else None
        tracks_ready = bool(track is not None and track.worker is None
            and track.audition_ready and selection_dialog is not None
            and selection_dialog.playback.is_advancing())
        if (selection_dialog is not None and (selection_dialog.loading or
                (not tracks_ready if args.smoke_tracks else not selection_context))
                and monotonic()<deadline):
            QTimer.singleShot(100,finish)
            return
        if roll_dialog is not None and roll_dialog.loading and monotonic() < deadline:
            QTimer.singleShot(100, finish)
            return
        if (args.smoke_context and roll_dialog is not None and not roll_dialog.loading
                and not roll_dialog.context_ready and monotonic() < deadline):
            QTimer.singleShot(100, finish)
            return
        if args.smoke_check_updates and not update_results and monotonic() < deadline:
            QTimer.singleShot(100, finish)
            return
        music_info: dict[str, object] = {}
        if music_dialog is not None:
            music_info = {'visible': music_dialog.isVisible(),
                'ready': music_dialog.worker is None,
                'image': not music_dialog.frame_image.isNull(),
                'clips': len(music_dialog.clips),
                'screenshot': music_dialog.grab().save(str(args.smoke_directory/'music.png'))}
        roll_info: dict[str, object] = {}
        if roll_dialog is not None and not roll_dialog.loading:
            roll_info = {"visible": roll_dialog.isVisible(),
                "changed": roll_dialog.roll.changed,
                "frames": roll_dialog.export_edit().plan.actual_frame_count,
                "context_button": roll_dialog.context_play.text(),
                "context_status": roll_dialog.context_status.text(),
                "context_rows": roll_dialog.context_rows,
                "context_ready": roll_dialog.context_ready,
                "context_source": roll_dialog.playback.player.source().toLocalFile(),
                "image": not roll_dialog.frame_image.isNull(),
                "button_enabled": window.roll_button.isEnabled(),
                "screenshot": roll_dialog.grab().save(str(args.smoke_directory / "roll.png"))}
            roll_dialog.reject()
        elif roll_dialog is not None:
            roll_info = {"visible": roll_dialog.isVisible(), "changed": False,
                         "image": False, "button_enabled": False, "screenshot": False,
                         "error": roll_dialog.loading_label.text()}
            roll_dialog.reject()
        selection_info: dict[str,object] = {}
        if selection_dialog is not None:
            window.grab().save(str(args.smoke_directory / "embedded.png"))
            selection_info = {"visible":selection_dialog.isVisible(),
                "embedded":not selection_dialog.isWindow(),
                "ready":not selection_dialog.loading,"context_prepared":bool(selection_context),
                "changed":not selection_dialog.loading and selection_dialog.session.changed,
                "image":not selection_dialog.frame_image.isNull(),
                "message":selection_dialog.message.text(),
                "screenshot":selection_dialog.grab().save(str(args.smoke_directory/"selection.png"))}
            if args.smoke_tracks and track is not None:
                selection_info['tracks'] = {
                    'ready': tracks_ready, 'embedded': not track.isWindow(),
                    'visible': track.isVisible(), 'clips': len(track.clips),
                    'independent': track.independent,
                    'muted_video': selection_dialog.playback.audio.isMuted(),
                    'audible_music': not track.audio.isMuted(),
                    'audio_path': track.player.source().toLocalFile(),
                    'video_lane_height': selection_dialog.montage_seek.height(),
                    'music_lane_height': track.lane.height(),
                    'two_tracks_height': selection_dialog.montage_seek.height()+track.height(),
                    'video_height': window.video.height(),
                    'shared_ruler': not track.lane.show_ruler,
                    'hover_checked': hover_checked,
                    'music_locked': track.locked,
                    'music_add_enabled': track.add_button.isEnabled(),
                    'music_split_positions': track.lane.split_positions,
                    'clock_error_ms': abs(track.player.position()-track.lane.value())}
            selection_dialog.reject()
        saved = window.grab().save(str(args.smoke_directory / "window.png"))
        frames = window.video_frames_seen
        (args.smoke_directory / "window.json").write_text(
            json.dumps(
                {
                    "visible": window.isVisible(),
                    "screenshot": saved,
                    "video_frames": frames,
                    "frozen": bool(getattr(sys, "frozen", False)),
                    "application_id": windows_application_id(),
                    "application_name": app.applicationName(),
                    "application_version": app.applicationVersion(),
                    "window_title": window.windowTitle(),
                    "version_label": window.version_label.text(),
                    "version_label_visible": window.version_label.isVisible(),
                    "window_icon_null": window.windowIcon().isNull(),
                    "icon_sizes": [size.width() for size in window.windowIcon().availableSizes()],
                    "language": window.locale().name(),
                    "create_label": window.create_button.text(),
                    "export_label": window.export_button.text(),
                    "export_enabled": window.export_button.isEnabled(),
                    "qt_cancel": QCoreApplication.translate("QPlatformTheme", "Cancel"),
                    "update_button_visible": window.update_button.isVisible(),
                    "update_button_icon": not window.update_button.icon().isNull(),
                    "update_tooltip": window.update_button.toolTip(),
                    "update_current_version": window.updates.checker.current,
                    "update_check_results": update_results,
                    "source_selection":selection_info,
                    'music_edit': music_info,
                    "episode_roll": roll_info,
                    "episode_roll_enabled": EPISODE_ROLL_ENABLED,
                    "episode_roll_button_visible": window.roll_button.isVisible(),
                }
            ),
            encoding="utf-8",
        )
        if music_dialog is not None:
            music_dialog.close()
        window.close()
        checked = not args.smoke_check_updates or (
            bool(update_results) and not update_results[0]["error"]
        )
        roll_ok = not roll_info or all(roll_info[key] for key in (
            "visible", "changed", "image", "button_enabled", "screenshot"))
        selection_ok = not selection_info or all(selection_info[k] for k in (
            "visible","ready","changed","image","screenshot")) and (
                tracks_ready if args.smoke_tracks else selection_info['context_prepared'])
        music_ok = not music_info or all(music_info[k] for k in ('visible','ready','image','clips',
                                                                'screenshot'))
        app.exit(0 if saved and checked and roll_ok and selection_ok and music_ok
                 and (not args.smoke_video or frames > 0 or music_info.get('image')) else 1)

    QTimer.singleShot(4000, finish)
    return app.exec()
