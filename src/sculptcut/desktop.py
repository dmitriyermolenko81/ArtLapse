"""Frozen entrypoint and opt-in diagnostic window launch."""

import argparse
import json
import sys
from pathlib import Path
from time import monotonic

from PySide6.QtCore import QCoreApplication, QSettings, QTimer, QUrl
from PySide6.QtWidgets import QApplication

from sculptcut.application_identity import configure_application_identity, windows_application_id
from sculptcut.cli import main as cli_main
from sculptcut.logging_config import configure_logging
from sculptcut.ui import MainWindow
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

    deadline = monotonic() + 12

    def finish() -> None:
        if args.smoke_check_updates and not update_results and monotonic() < deadline:
            QTimer.singleShot(100, finish)
            return
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
                    "window_icon_null": window.windowIcon().isNull(),
                    "icon_sizes": [size.width() for size in window.windowIcon().availableSizes()],
                    "language": window.locale().name(),
                    "create_label": window.create_button.text(),
                    "qt_cancel": QCoreApplication.translate("QPlatformTheme", "Cancel"),
                    "update_button_visible": window.update_button.isVisible(),
                    "update_button_icon": not window.update_button.icon().isNull(),
                    "update_tooltip": window.update_button.toolTip(),
                    "update_current_version": window.updates.checker.current,
                    "update_check_results": update_results,
                }
            ),
            encoding="utf-8",
        )
        window.close()
        checked = not args.smoke_check_updates or (
            bool(update_results) and not update_results[0]["error"]
        )
        app.exit(0 if saved and checked and (not args.smoke_video or frames > 0) else 1)

    QTimer.singleShot(4000, finish)
    return app.exec()
