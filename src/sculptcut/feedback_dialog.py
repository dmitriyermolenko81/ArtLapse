"""Non-blocking feedback form with explicit sending and a recoverable local draft."""

import json
from uuid import UUID, uuid4

from PySide6.QtCore import QByteArray, QSettings, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from sculptcut.feedback import (
    MAX_BODY_BYTES,
    MAX_MESSAGE,
    Feedback,
    FeedbackDraft,
    feedback_endpoint,
    new_reply_token,
    report_text,
    technical_context,
    telegram_reply_url,
    validate_endpoint,
)
from sculptcut.i18n import tr
from sculptcut.ui_theme import apply_dialog_theme, dialog_header


class FeedbackDialog(QDialog):
    def __init__(
        self, settings: QSettings, mode: str, parent: QWidget | None = None, *,
        endpoint: str | None = None, allow_local_http: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Обратная связь — SculptCut"))
        self.resize(520, 360)
        apply_dialog_theme(self)
        self.settings = settings
        self.mode = mode
        self.endpoint = feedback_endpoint() if endpoint is None else validate_endpoint(
            endpoint, allow_local_http=allow_local_http,
        )
        self.report_id = uuid4()
        self.signature = ""
        self.reply_token: str | None = None
        self.last_reply_token = str(settings.value("feedback_reply_token", ""))
        try:
            telegram_reply_url(self.last_reply_token)
        except ValueError:
            self.last_reply_token = ""
        self.sent = False
        self.reply: QNetworkReply | None = None
        self.network = QNetworkAccessManager(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        layout.addWidget(dialog_header(tr("Обратная связь"), "feedback"))
        self.kind = QComboBox()
        self.kind.setProperty("controlRole", "menu")
        for label, key in ((tr("Сообщить об ошибке"), "bug"), (tr("Предложить улучшение"), "idea"),
                           (tr("Другое"), "other")):
            self.kind.addItem(label, key)
        self.kind.setAccessibleName(tr("Тип отзыва"))
        self.kind.setToolTip(tr("Текст отзыва получат участники группы SculptCut в Telegram."))
        layout.addWidget(self.kind)
        self.message = QTextEdit()
        self.message.setAcceptRichText(False)
        self.message.setPlaceholderText(
            tr("Что произошло? Что вы делали перед этим и какой результат ожидали?")
        )
        self.message.setAccessibleName(tr("Описание ошибки или предложения"))
        layout.addWidget(self.message, 1)
        self.counter = QLabel()
        self.counter.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.counter.setObjectName("subtitle")
        layout.addWidget(self.counter)
        reply_actions = QHBoxLayout()
        self.telegram_optin = QCheckBox(tr("Ответ в Telegram"))
        self.telegram_optin.setAccessibleName(tr("Получить ответ в Telegram"))
        self.telegram_optin.setToolTip(
            tr("После отправки откройте бота\nи нажмите «Начать».\n\n"
            "Ответы придут в личный чат.\n"
            "Ваши сообщения получит группа отзывов.\n\n"
            "Связь действует 30 дней.\n/stop в боте отключает переписку.")
        )
        reply_actions.addWidget(self.telegram_optin)
        reply_actions.addStretch()
        self.telegram_button = QPushButton(tr("Открыть бота"))
        self.telegram_button.setToolTip(
            tr("Открыть последнее обращение и нажать «Начать» в Telegram")
        )
        self.telegram_button.clicked.connect(self.open_telegram)
        reply_actions.addWidget(self.telegram_button)
        layout.addLayout(reply_actions)
        self.extra = QWidget()
        extra_layout = QVBoxLayout(self.extra)
        extra_layout.setContentsMargins(0, 0, 0, 0)
        extra_layout.setSpacing(8)
        self.contact = QLineEdit()
        self.contact.setMaxLength(150)
        self.contact.setPlaceholderText(tr("Telegram или почта — необязательно"))
        self.contact.setAccessibleName(tr("Контакт для ответа"))
        extra_layout.addWidget(self.contact)
        self.context = technical_context(mode)
        self.include_context = QCheckBox(tr("Добавить сведения о программе"))
        self.include_context.setToolTip(
            tr(f"Версия: {self.context.version}\nСистема: {self.context.system}\nРежим: {mode}\n"
            "Видео, проекты и журналы не отправляются.")
        )
        extra_layout.addWidget(self.include_context)
        self.copy_button = QPushButton(tr("Скопировать текст"))
        self.copy_button.clicked.connect(self.copy_report)
        extra_layout.addWidget(self.copy_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.extra.hide()
        layout.addWidget(self.extra)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status.hide()
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.more_button = QPushButton(tr("Дополнительно"))
        self.more_button.setCheckable(True)
        self.more_button.setFlat(True)
        self.more_button.setProperty("actionRole", "utility")
        self.more_button.setAutoDefault(False)
        self.more_button.setToolTip(tr("Контакт, сведения о программе и копирование текста"))
        self.more_button.toggled.connect(self.toggle_extra)
        actions.addWidget(self.more_button)
        actions.addStretch()
        self.close_button = QPushButton(tr("Закрыть"))
        self.close_button.clicked.connect(self.close)
        actions.addWidget(self.close_button)
        self.send_button = QPushButton(tr("Отправить"))
        self.send_button.setObjectName("primary")
        self.send_button.setAutoDefault(False)
        self.send_button.clicked.connect(self.send)
        actions.addWidget(self.send_button)
        layout.addLayout(actions)
        self.restore_draft()
        for signal in (self.message.textChanged, self.contact.textChanged,
                       self.kind.currentIndexChanged, self.include_context.toggled,
                       self.telegram_optin.toggled):
            signal.connect(self.refresh)
        if not self.endpoint:
            self.set_status(
                tr("Приём отзывов пока не подключён. Можно подготовить и скопировать текст.")
            )
            self.more_button.setChecked(True)
        self.refresh()

    def set_status(self, message: str) -> None:
        self.status.setText(message)
        self.status.setVisible(bool(message))

    def toggle_extra(self, expanded: bool) -> None:
        self.extra.setVisible(expanded)
        self.more_button.setText(tr("Скрыть") if expanded else tr("Дополнительно"))
        if self.isVisible():
            extra_height = self.extra.sizeHint().height() + 10 if expanded else 0
            self.resize(self.width(), 360 + extra_height)

    def restore_draft(self) -> None:
        try:
            raw = str(self.settings.value("feedback_draft", ""))
            if len(raw) > MAX_BODY_BYTES:
                return
            draft = json.loads(raw)
            report = FeedbackDraft.model_validate(draft)
        except (ValueError, TypeError):
            return
        self.report_id = report.id
        self.signature = report.model_dump_json(exclude={"id"})
        self.kind.setCurrentIndex(max(0, self.kind.findData(report.kind)))
        self.message.setPlainText(report.message)
        self.contact.setText(report.contact)
        self.include_context.setChecked(report.context is not None)
        self.reply_token = report.reply_token
        self.telegram_optin.setChecked(report.reply_token is not None)
        self.more_button.setChecked(bool(report.contact or report.context))

    def payload(self) -> Feedback:
        return Feedback.model_validate(self.draft_payload().model_dump())

    def draft_payload(self) -> FeedbackDraft:
        return FeedbackDraft(id=self.report_id, kind=self.kind.currentData(),
                             message=self.message.toPlainText().strip(),
                             contact=self.contact.text().strip(),
                             context=self.context if self.include_context.isChecked() else None,
                             reply_token=(self.reply_token
                                          if self.telegram_optin.isChecked() else None))

    def refresh(self) -> None:
        if self.telegram_optin.isChecked() and self.reply_token is None:
            self.reply_token = new_reply_token()
        self.message.setPlaceholderText({
            "bug": tr("Что произошло? Что вы делали перед этим и какой результат ожидали?"),
            "idea": tr("Что стоит добавить или изменить? Чем это поможет в вашей работе?"),
            "other": tr("Что хотите рассказать о работе SculptCut?"),
        }[self.kind.currentData()])
        length = len(self.message.toPlainText().strip())
        self.counter.setText(f"{length} / {MAX_MESSAGE}")
        self.counter.setVisible(length > MAX_MESSAGE - 200)
        self.send_button.setEnabled(bool(self.endpoint) and 10 <= length <= MAX_MESSAGE
                                    and self.reply is None and not self.sent)
        self.copy_button.setEnabled(10 <= length <= MAX_MESSAGE)
        self.telegram_button.setVisible(bool(
            (self.sent and self.telegram_optin.isChecked() and self.reply_token)
            or (not self.telegram_optin.isChecked() and self.last_reply_token)
        ))

    def open_telegram(self) -> None:
        token = (self.reply_token if self.sent and self.telegram_optin.isChecked()
                 else self.last_reply_token)
        if not token:
            return
        if not QDesktopServices.openUrl(QUrl(telegram_reply_url(token))):
            self.set_status(tr("Не удалось открыть Telegram. Проверьте обработчик ссылок."))

    def copy_report(self) -> None:
        QApplication.clipboard().setText(report_text(self.payload()))
        self.set_status(tr("Текст отзыва скопирован."))

    def save_draft(self) -> None:
        if self.sent:
            self.settings.remove("feedback_draft")
        elif len(self.message.toPlainText().strip()) <= MAX_MESSAGE:
            self.settings.setValue("feedback_draft", self.draft_payload().model_dump_json())
        self.settings.sync()

    def send(self) -> None:
        if not self.send_button.isEnabled():
            return
        report = self.payload()
        signature = report.model_dump_json(exclude={"id"})
        if self.signature and self.signature != signature:
            self.report_id = uuid4()
            if self.telegram_optin.isChecked():
                self.reply_token = new_reply_token()
            report = self.payload()
        self.signature = report.model_dump_json(exclude={"id"})
        self.save_draft()
        request = QNetworkRequest(QUrl(self.endpoint))
        request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
        request.setTransferTimeout(25_000)
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        self.reply = self.network.post(
            request, QByteArray(report.model_dump_json(exclude_none=True).encode("utf-8")),
        )
        self.reply.setReadBufferSize(MAX_BODY_BYTES + 1)
        self.reply.readyRead.connect(self.check_response_size)
        self.reply.finished.connect(self.delivery_finished)
        for control in (self.kind, self.message, self.contact, self.include_context,
                        self.telegram_optin):
            control.setEnabled(False)
        self.set_status(tr("Отправляем отзыв…"))
        self.refresh()

    def check_response_size(self) -> None:
        if self.reply is not None and self.reply.bytesAvailable() > MAX_BODY_BYTES:
            self.reply.abort()

    def delivery_finished(self) -> None:
        reply = self.reply
        if reply is None:
            return
        self.reply = None
        code = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        try:
            if reply.bytesAvailable() > MAX_BODY_BYTES:
                raise ValueError("Invalid server response")
            result = json.loads(bytes(reply.readAll().data()))
            self.sent = (code == 200 and reply.error() == QNetworkReply.NetworkError.NoError
                         and result.get("ok") is True
                         and UUID(result.get("id", "")) == self.report_id)
        except (ValueError, TypeError, AttributeError):
            self.sent = False
        error = reply.error()
        reply.deleteLater()
        if self.sent:
            if self.telegram_optin.isChecked() and self.reply_token:
                self.last_reply_token = self.reply_token
                self.settings.setValue("feedback_reply_token", self.reply_token)
                status = tr("Отзыв отправлен. В боте нажмите «Начать».")
            else:
                status = tr("Отзыв отправлен. Спасибо!")
        elif code == 429:
            status = tr("Слишком много попыток (HTTP 429). Попробуйте позже; текст сохранён.")
        elif isinstance(code, int):
            detail = {
                202: tr("сервис ещё не подтвердил отправку"),
                400: tr("приёмник отклонил данные отзыва"),
                403: tr("доступ к приёмнику запрещён"),
                502: tr("приёмник не смог передать отзыв в Telegram"),
                503: tr("приёмник временно недоступен"),
            }.get(code, tr("ошибка ответа сервера"))
            status = (tr(f"Не удалось подтвердить доставку: {detail} (HTTP {code}). "
                      "Текст сохранён."))
        else:
            detail = (tr("ошибка защищённого соединения SSL")
                      if error == QNetworkReply.NetworkError.SslHandshakeFailedError
                      else tr("ошибка подключения к серверу"))
            status = (tr(f"Не удалось подтвердить доставку: {detail} "
                      f"(код сети {error.value}). Текст сохранён."))
        self.set_status(status)
        for control in (self.kind, self.message, self.contact, self.include_context,
                        self.telegram_optin):
            control.setEnabled(not self.sent)
        self.save_draft()
        self.refresh()

    def done(self, result: int) -> None:
        self.save_draft()
        super().done(result)
