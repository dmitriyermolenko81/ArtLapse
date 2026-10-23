"""Frozen entrypoint and opt-in diagnostic window launch."""

import argparse
import json
import sys
from pathlib import Path

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
    if args.smoke_video:
        window.player.setSource(QUrl.fromLocalFile(str(args.smoke_video.resolve())))
        window.player.play()

    def finish() -> None:
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
                }
            ),
            encoding="utf-8",
        )
        window.close()
        app.exit(0 if saved and (not args.smoke_video or frames > 0) else 1)

    QTimer.singleShot(4000, finish)
    return app.exec()
