"""Bounded asynchronous release checks and a compact optional download prompt."""

import json
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from sculptcut import __version__
from sculptcut.i18n import tr
from sculptcut.ui_theme import apply_dialog_theme
from sculptcut.updates import (
    CHECK_TIMEOUT_MS,
    LATEST_API_URL,
    MAX_RESPONSE_BYTES,
    STARTUP_DELAY_MS,
    ReleaseUpdate,
    available_update,
)


class UpdateChecker(QObject):
    completed = Signal(object, str)
    checking_changed = Signal(bool)

    def __init__(self, parent: QObject | None = None, *, endpoint: str = LATEST_API_URL,
                 current: str = __version__, timeout_ms: int = CHECK_TIMEOUT_MS,
                 allow_local_http: bool = False) -> None:
        super().__init__(parent)
        url = urlsplit(endpoint)
        local = (allow_local_http and url.scheme == "http" and url.hostname == "127.0.0.1"
                 and not url.username and not url.password)
        if endpoint != LATEST_API_URL and not local:
            raise ValueError("Update checks use the official release API")
        self.endpoint = endpoint
        self.current = current
        self.timeout_ms = timeout_ms
        self.network = QNetworkAccessManager(self)
        self.reply: QNetworkReply | None = None
        self.buffer = bytearray()
        self.failure = ""
        self.closed = False
        self.finishing = False
        self.deadline = QTimer(self)
        self.deadline.setSingleShot(True)
        self.deadline.timeout.connect(self._timeout)

    def start(self) -> None:
        if self.closed or self.reply is not None:
            return
        self.buffer.clear()
        self.failure = ""
        request = QNetworkRequest(QUrl(self.endpoint))
        request.setRawHeader(b"User-Agent", f"ArtLapse/{self.current}".encode("ascii"))
        request.setRawHeader(b"Accept", b"application/vnd.github+json")
        request.setRawHeader(b"X-GitHub-Api-Version", b"2022-11-28")
        request.setTransferTimeout(self.timeout_ms)
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        self.reply = self.network.get(request)
        self.reply.setReadBufferSize(MAX_RESPONSE_BYTES + 1)
        self.reply.readyRead.connect(self._read)
        self.reply.finished.connect(self._finished)
        self.deadline.start(self.timeout_ms)
        self.checking_changed.emit(True)

    def _read(self) -> None:
        if self.reply is None or self.failure:
            return
        self.buffer.extend(self.reply.read(MAX_RESPONSE_BYTES + 1 - len(self.buffer)).data())
        if len(self.buffer) > MAX_RESPONSE_BYTES:
            self.failure = "Release response is too large"
            self.reply.abort()

    def _timeout(self) -> None:
        if self.reply is not None:
            self.failure = "Update check timed out"
            self.reply.abort()

    def _finished(self) -> None:
        if self.reply is None or self.finishing:
            return
        self.finishing = True
        reply = self.reply
        self.deadline.stop()
        self._read()
        release: ReleaseUpdate | None = None
        error = self.failure
        if not error:
            if (reply.error() != QNetworkReply.NetworkError.NoError
                    or reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) != 200):
                error = "Release API unavailable"
            else:
                try:
                    release = available_update(json.loads(self.buffer), self.current)
                except (ValueError, UnicodeError, RecursionError):
                    error = "Invalid release response"
        self.reply = None
        self.buffer.clear()
        self.finishing = False
        reply.deleteLater()
        if not self.closed:
            self.checking_changed.emit(False)
            self.completed.emit(release, error)

    def shutdown(self) -> None:
        self.closed = True
        self.deadline.stop()
        reply, self.reply = self.reply, None
        if reply is not None:
            reply.abort()
            reply.deleteLater()


class UpdateDialog(QDialog):
    def __init__(self, parent: QWidget, message: str, *, release: ReleaseUpdate | None = None,
                 current: str = __version__) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Обновления ArtLapse"))
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowModality(Qt.WindowModality.NonModal)
        apply_dialog_theme(self)
        self.setFixedWidth(380)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 14)
        layout.setSpacing(12)
        self.message = QLabel(message)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        if release is not None:
            installed = QLabel(tr(f"Установлена версия {current}"))
            installed.setObjectName("sectionHint")
            layout.addWidget(installed)
        buttons = QHBoxLayout()
        buttons.addStretch()
        later = QPushButton(tr("Позже") if release else tr("Закрыть"))
        later.clicked.connect(self.reject)
        buttons.addWidget(later)
        self.download_button: QPushButton | None = None
        if release is not None:
            self.download_button = QPushButton(tr("Скачать"))
            self.download_button.setObjectName("primary")
            self.download_button.clicked.connect(lambda: self.download(release))
            buttons.addWidget(self.download_button)
        layout.addLayout(buttons)

    def download(self, release: ReleaseUpdate) -> None:
        if QDesktopServices.openUrl(QUrl(release.download_url)):
            self.accept()
        else:
            self.message.setText(tr("Не удалось открыть браузер. Попробуйте ещё раз."))


class UpdateController(QObject):
    def __init__(self, window: QWidget, button: QPushButton,
                 checker: UpdateChecker | None = None) -> None:
        super().__init__(window)
        self.window = window
        self.button = button
        self.checker = checker or UpdateChecker(self)
        self.manual = False
        self.closed = False
        self.dialog: UpdateDialog | None = None
        self.startup = QTimer(self)
        self.startup.setSingleShot(True)
        self.startup.timeout.connect(lambda: self.check(manual=False))
        button.clicked.connect(lambda: self.check(manual=True))
        self.checker.checking_changed.connect(self._checking)
        self.checker.completed.connect(self._completed)

    def schedule_startup(self) -> None:
        if not self.closed:
            self.startup.start(STARTUP_DELAY_MS)

    def check(self, *, manual: bool) -> None:
        if self.closed:
            return
        self.startup.stop()
        self.manual = self.manual or manual
        self.checker.start()

    def _checking(self, active: bool) -> None:
        self.button.setEnabled(not active)
        self.button.setToolTip(tr("Проверка обновлений…") if active
                               else tr("Проверить обновления"))

    def _completed(self, release: object, error: str) -> None:
        manual, self.manual = self.manual, False
        if self.closed:
            return
        update = release if isinstance(release, ReleaseUpdate) else None
        if error:
            if not manual:
                return
            message = tr("Не удалось проверить обновления. Проверьте интернет и попробуйте позже.")
        elif update is not None:
            message = tr(f"Доступна ArtLapse {update.version}")
        elif manual:
            message = tr(
                f"Новых версий для Windows нет. Установлена ArtLapse {self.checker.current}.",
            )
        else:
            return
        if self.dialog is not None:
            self.dialog.reject()
        self.dialog = UpdateDialog(self.window, message, release=update,
                                   current=self.checker.current)
        self.dialog.finished.connect(self._dialog_finished)
        self.dialog.show()

    def _dialog_finished(self, result: int) -> None:
        self.dialog = None

    def shutdown(self) -> None:
        self.closed = True
        self.startup.stop()
        self.checker.shutdown()
        if self.dialog is not None:
            self.dialog.reject()
