"""Owner-only webhook setup. The bot key is entered locally and never saved or logged."""

import hashlib
import hmac
import json
import re
from collections.abc import Callable

from PySide6.QtCore import QByteArray, QEventLoop, QTimer, QUrl
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QInputDialog,
    QLabel,
    QLineEdit,
    QProgressDialog,
    QPushButton,
)

from sculptcut.ui_theme import STYLE, show_notice, style_setup_dialog

RECEIVER = "https://your-feedback-worker.example"
SECRET_LABEL = b"SculptCut Telegram webhook v1"
Transport = Callable[[str, dict[str, object] | None], dict[str, object]]


class SetupError(Exception):
    """A safe, user-facing error without credentials or request URLs."""


def connect(token: str, request: Transport) -> None:
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
        raise SetupError("Нужен ключ бота, выданный BotFather.")
    health = request(RECEIVER + "/", None)
    revision = health.get("revision")
    if (health.get("telegram_replies") is not True or health.get("ready") is not True
            or not isinstance(revision, int) or revision < 3):
        raise SetupError("Сначала обновите schema.sql и worker.mjs в Cloudflare до версии 3.")

    def api(method: str, payload: dict[str, object]) -> object:
        result = request("https://api.telegram.org/bot" + token + "/" + method, payload)
        if result.get("ok") is not True:
            raise SetupError("Telegram отклонил настройку. Проверьте ключ и повторите подключение.")
        return result.get("result")

    identity = api("getMe", {})
    if not isinstance(identity, dict) or identity.get("username", "").lower() != "sculptcut_bot":
        raise SetupError("Этот ключ принадлежит другому боту. Нужен ключ @SculptCut_bot.")
    target = RECEIVER + "/telegram"
    current = api("getWebhookInfo", {})
    if not isinstance(current, dict):
        raise SetupError("Telegram не подтвердил состояние подключения.")
    if current.get("url") and current["url"] != target:
        raise SetupError("У бота уже подключён другой обработчик. Настройка не изменена.")
    secret = hmac.new(token.encode(), SECRET_LABEL, hashlib.sha256).hexdigest()
    if api("setWebhook", {"url": target, "secret_token": secret,
                          "allowed_updates": ["message"], "max_connections": 1}) is not True:
        raise SetupError("Telegram не подтвердил подключение. Повторите настройку.")
    confirmed = api("getWebhookInfo", {})
    if not isinstance(confirmed, dict) or confirmed.get("url") != target:
        raise SetupError("Подключение пока не подтверждено. Повторите настройку.")


class QtTransport:
    def __init__(self, progress: QProgressDialog) -> None:
        self.progress = progress
        self.network = QNetworkAccessManager(progress)

    def __call__(self, url: str, payload: dict[str, object] | None) -> dict[str, object]:
        if self.progress.wasCanceled():
            raise SetupError("Настройка отменена. При необходимости запустите её снова.")
        request = QNetworkRequest(QUrl(url))
        request.setTransferTimeout(15_000)
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        if payload is None:
            reply = self.network.get(request)
        else:
            request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
            reply = self.network.post(request, QByteArray(json.dumps(payload).encode()))
        reply.setReadBufferSize(65_537)
        reply.readyRead.connect(lambda: reply.abort() if reply.bytesAvailable() > 65_536 else None)
        loop = QEventLoop()
        reply.finished.connect(loop.quit)
        self.progress.canceled.connect(reply.abort)
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(reply.abort)
        timer.start(20_000)
        if not reply.isFinished():
            loop.exec()
        timer.stop()
        self.progress.canceled.disconnect(reply.abort)
        try:
            status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            if (status != 200 or reply.error() != QNetworkReply.NetworkError.NoError
                    or reply.bytesAvailable() > 65_536):
                raise SetupError("Не удалось связаться с сервисом. Проверьте подключение и ключ. "
                                 "Если настройка прервалась, её можно безопасно повторить.")
            data = json.loads(bytes(reply.readAll().data()))
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (ValueError, TypeError):
            raise SetupError("Сервис вернул неподтверждённый ответ. Повторите настройку.") from None
        finally:
            reply.deleteLater()


def main() -> None:
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(STYLE)
    dialog = QInputDialog()
    dialog.setWindowTitle("Ответы через Telegram — SculptCut")
    dialog.setLabelText(
        "Сначала обновите таблицы D1 и код Worker в Cloudflare.\n"
        "Вставьте ключ @SculptCut_bot из BotFather.\n"
        "Он используется только для настройки Telegram и не сохраняется."
    )
    dialog.setTextEchoMode(QLineEdit.EchoMode.Password)
    dialog.setOkButtonText("Подключить ответы")
    dialog.setCancelButtonText("Отмена")
    dialog.setMinimumWidth(540)
    style_setup_dialog(dialog, "Ответы через Telegram")
    counter = QLabel("Ключ ещё не введён")
    paste = QPushButton("Вставить ключ из буфера")
    paste.clicked.connect(lambda: dialog.setTextValue(app.clipboard().text().strip()))
    dialog.textValueChanged.connect(
        lambda text: counter.setText(f"Введено символов: {len(text.strip())}")
    )
    dialog.layout().addWidget(counter)
    dialog.layout().addWidget(paste)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return
    token = dialog.textValue().strip()
    dialog.setTextValue("")
    progress = QProgressDialog("Подключаем ответы через @SculptCut_bot…", "Отмена", 0, 0)
    progress.setWindowTitle("SculptCut")
    progress.setMinimumDuration(0)
    progress.show()
    try:
        connect(token, QtTransport(progress))
    except SetupError as error:
        progress.close()
        show_notice(None, "Подключение ответов", str(error))
    else:
        progress.close()
        show_notice(None, "Ответы подключены",
                                "Telegram подтвердил подключение.\n\n"
                                "В SculptCut отправьте отзыв с галочкой ответа, откройте бота "
                                "и нажмите «Начать». Затем ответьте на отзыв в группе "
                                "от своего личного аккаунта владельца группы.")
    finally:
        token = ""


if __name__ == "__main__":
    main()
