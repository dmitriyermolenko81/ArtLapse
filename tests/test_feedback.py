import hashlib
import hmac
import json
import runpy
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from time import monotonic
from urllib.request import Request, urlopen

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtGui import QDesktopServices
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from sculptcut.feedback import (
    MAX_MESSAGE,
    feedback_endpoint,
    report_text,
    telegram_reply_url,
    validate_endpoint,
)
from sculptcut.feedback_dialog import FeedbackDialog


@pytest.fixture
def relay() -> Iterator[str]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Local feedback integration requires Node.js with node:sqlite")
    script = Path(__file__).resolve().parents[1] / "services/feedback/local-test-server.mjs"
    process = subprocess.Popen([node, str(script)], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout is not None
        line = process.stdout.readline()
        assert line, "Local feedback server did not start"
        yield f"http://127.0.0.1:{json.loads(line)['port']}"
    finally:
        process.terminate()
        process.communicate(timeout=5)


def settle(dialog: FeedbackDialog) -> None:
    deadline = monotonic() + 8
    while dialog.reply is not None and monotonic() < deadline:
        QTest.qWait(10)
    assert dialog.reply is None


def test_form_sends_to_worker_and_telegram_over_real_local_http(
    tmp_path: Path, relay: str,
) -> None:
    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "feedback.ini"), QSettings.Format.IniFormat)
    dialog = FeedbackDialog(settings, "music_shorts", endpoint=relay + "/feedback",
                            allow_local_http=True)
    try:
        dialog.show()
        app.processEvents()
        assert not dialog.send_button.isEnabled()
        assert not dialog.include_context.isChecked()
        assert not dialog.extra.isVisible()
        assert not dialog.counter.isVisible()
        dialog.more_button.click()
        assert dialog.contact.isVisible() and dialog.include_context.isVisible()
        dialog.message.setPlainText("Предлагаю добавить удобный поиск нужного эпизода")
        dialog.contact.setText("@feedback_tester")
        copied_text = report_text(dialog.payload())
        assert str(dialog.report_id) not in copied_text
        assert copied_text.startswith("Ошибка\n\n")
        assert dialog.send_button.isEnabled()
        dialog.send_button.click()
        dialog.send()  # A second click while posting must not send another request.
        settle(dialog)
        assert dialog.sent and "Отзыв отправлен" in dialog.status.text()
        assert not settings.contains("feedback_draft")
        with urlopen(relay + "/messages", timeout=5) as response:
            messages = json.load(response)
        assert len(messages) == 1
        assert messages[0]["chat_id"] == "-1001234567890"
        assert "@feedback_tester" in messages[0]["text"]
        assert messages[0]["text"] == copied_text
        assert "Версия:" not in messages[0]["text"]
        assert str(tmp_path) not in messages[0]["text"]
    finally:
        dialog.close()
        app.processEvents()


def test_delivery_failure_keeps_draft_and_retry_can_confirm_success(
    tmp_path: Path, relay: str,
) -> None:
    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "feedback.ini"), QSettings.Format.IniFormat)
    with urlopen(relay + "/fail", timeout=5):
        pass
    dialog = FeedbackDialog(settings, "shorts", endpoint=relay + "/feedback",
                            allow_local_http=True)
    dialog.message.setPlainText("При экспорте ролика обнаружена ошибка в эпизоде")
    dialog.include_context.setChecked(True)
    try:
        original_id = dialog.report_id
        dialog.send_button.click()
        settle(dialog)
        assert not dialog.sent
        assert "Не удалось подтвердить" in dialog.status.text()
        assert "HTTP 502" in dialog.status.text()
        assert "Telegram" in dialog.status.text()
        assert settings.contains("feedback_draft")
        assert dialog.message.isEnabled()
        dialog.close()
        restored = FeedbackDialog(settings, "shorts", endpoint=relay + "/feedback",
                                  allow_local_http=True)
        try:
            assert restored.report_id == original_id
            assert restored.include_context.isChecked()
            with urlopen(relay + "/recover", timeout=5):
                pass
            restored.send_button.click()
            settle(restored)
            assert restored.sent
            with urlopen(relay + "/messages", timeout=5) as response:
                messages = json.load(response)
            assert len(messages) == 1
            assert "Версия:" in messages[0]["text"]
        finally:
            restored.close()
    finally:
        dialog.close()
        app.processEvents()


def test_network_failure_has_a_distinct_error_and_keeps_draft(
    tmp_path: Path, relay: str,
) -> None:
    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "feedback.ini"), QSettings.Format.IniFormat)
    dialog = FeedbackDialog(settings, "shorts", endpoint=relay + "/disconnect",
                            allow_local_http=True)
    try:
        dialog.message.setPlainText("Тест недоступного сетевого соединения")
        dialog.send_button.click()
        settle(dialog)
        assert not dialog.sent
        assert "код сети" in dialog.status.text()
        assert "HTTP" not in dialog.status.text()
        assert settings.contains("feedback_draft")
        assert dialog.send_button.isEnabled()
        with urlopen(relay + "/messages", timeout=5) as response:
            assert json.load(response) == []
    finally:
        dialog.close()
        app.processEvents()


def test_configuration_and_unconnected_form_do_not_claim_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCULPTCUT_FEEDBACK_ENDPOINT", "https://feedback.example/feedback")
    assert feedback_endpoint() == "https://feedback.example/feedback"
    monkeypatch.setenv("SCULPTCUT_FEEDBACK_ENDPOINT", "http://example.com/feedback")
    assert feedback_endpoint() == ""
    for url in ("http://example.com/feedback", "https://user:secret@example.com/feedback",
                "https://example.com/feedback?token=secret", "file:///tmp/feedback"):
        with pytest.raises(ValueError):
            validate_endpoint(url)
    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "feedback.ini"), QSettings.Format.IniFormat)
    dialog = FeedbackDialog(settings, "shorts")
    try:
        dialog.message.setPlainText("Предлагаю улучшить удобство выбора кадров")
        assert not dialog.send_button.isEnabled()
        assert "не подключён" in dialog.status.text()
        assert dialog.copy_button.isEnabled()
        dialog.message.setPlainText("a" * (MAX_MESSAGE + 1))
        assert not dialog.send_button.isEnabled() and not dialog.copy_button.isEnabled()
    finally:
        dialog.close()
        app.processEvents()


def test_optin_restores_link_and_owner_reply_reaches_the_right_user_over_http(
    tmp_path: Path, relay: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "feedback.ini"), QSettings.Format.IniFormat)
    settings.setValue("feedback_reply_token", "https://untrusted.invalid/")
    dialog = FeedbackDialog(settings, "shorts", endpoint=relay + "/feedback",
                            allow_local_http=True)
    opened: list[str] = []
    monkeypatch.setattr(QDesktopServices, "openUrl",
                        lambda url: opened.append(url.toString()) or True)
    try:
        dialog.show()
        app.processEvents()
        assert not dialog.telegram_optin.isChecked()
        assert not dialog.telegram_button.isVisible()
        dialog.message.setPlainText("Ошибка при выборе музыкального эпизода, нужен ответ")
        dialog.telegram_optin.setChecked(True)
        token = dialog.reply_token
        assert token is not None and len(token) == 43
        dialog.save_draft()
        restored = FeedbackDialog(settings, "shorts", endpoint=relay + "/feedback",
                                  allow_local_http=True)
        assert restored.reply_token == token and restored.telegram_optin.isChecked()
        restored.close()
        dialog.send_button.click()
        settle(dialog)
        assert dialog.sent and dialog.telegram_button.isVisible()
        assert "Начать" in dialog.status.text()
        dialog.telegram_button.click()
        assert opened == [telegram_reply_url(token)]
        assert settings.value("feedback_reply_token") == token
        signature = hmac.new(b"12345:test_secret", b"SculptCut Telegram webhook v1",
                             hashlib.sha256).hexdigest()

        def hook(update: dict[str, object]) -> None:
            request = Request(relay + "/telegram", data=json.dumps(update).encode(), headers={
                "Content-Type": "application/json",
                "X-Telegram-Bot-Api-Secret-Token": signature,
            })
            with urlopen(request, timeout=5) as response:
                assert response.status == 200

        hook({"update_id": 10, "message": {"message_id": 10, "from": {"id": 10101},
              "chat": {"id": 10101, "type": "private"}, "text": "/start r_" + token}})
        answer = {"update_id": 11, "message": {"message_id": 11, "from": {"id": 9001},
                  "chat": {"id": -1001234567890, "type": "supergroup"},
                  "text": "Исправили, попробуйте ещё раз", "reply_to_message": {"message_id": 1}}}
        hook(answer)
        hook(answer)  # A Telegram webhook retry must not duplicate the owner's reply.
        hook({"update_id": 12, "message": {"message_id": 12, "from": {"id": 10101},
              "chat": {"id": 10101, "type": "private"}, "text": "Теперь всё работает"}})
        with urlopen(relay + "/messages", timeout=5) as response:
            messages = json.load(response)
        replies = [m for m in messages if "Исправили" in m["text"]]
        assert len(replies) == 1 and replies[0]["chat_id"] == "10101"
        followup = next(m for m in messages if "Теперь всё работает" in m["text"])
        assert followup["chat_id"] == "-1001234567890"
        assert followup["reply_parameters"]["message_id"] == 1
        assert token not in json.dumps(messages)
        assert str(dialog.report_id) not in messages[0]["text"]
        assert "Автор выбрал" not in messages[0]["text"]
        reopened = FeedbackDialog(settings, "shorts", endpoint=relay + "/feedback",
                                  allow_local_http=True)
        reopened.show()
        app.processEvents()
        assert reopened.telegram_button.isVisible()
        reopened.close()
    finally:
        dialog.close()
        app.processEvents()


def test_owner_setup_checks_identity_and_preserves_pending_updates() -> None:
    helper = runpy.run_path(str(Path(__file__).resolve().parents[1]
                               / "services/feedback/connect_replies.py"))
    calls: list[tuple[str, object]] = []
    target = helper["RECEIVER"] + "/telegram"
    token = "12345:test_secret"
    state = {"url": "", "username": "SculptCut_bot", "revision": 3}

    def transport(url: str, payload: dict[str, object] | None) -> dict[str, object]:
        calls.append((url, payload))
        if payload is None:
            return {"revision": state["revision"], "ready": True, "telegram_replies": True}
        if url.endswith("/getMe"):
            return {"ok": True, "result": {"username": state["username"]}}
        if url.endswith("/setWebhook"):
            assert payload == {"url": target, "allowed_updates": ["message"], "max_connections": 1,
                               "secret_token": hmac.new(token.encode(), helper["SECRET_LABEL"],
                                                        hashlib.sha256).hexdigest()}
            state["url"] = target
            return {"ok": True, "result": True}
        assert url.endswith("/getWebhookInfo")
        return {"ok": True, "result": {"url": state["url"]}}

    helper["connect"](token, transport)
    assert state["url"] == target
    for changes in ({"username": "other_bot"}, {"revision": 2},
                    {"url": "https://other.invalid/hook"}):
        state.update(url="", username="SculptCut_bot", revision=3)
        state.update(changes)
        calls.clear()
        with pytest.raises(helper["SetupError"]):
            helper["connect"](token, transport)
        assert not any(url.endswith("/setWebhook") for url, _ in calls)
