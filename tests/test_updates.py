"""Release selection and real asynchronous HTTP checks without changing user files."""

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from time import monotonic

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from sculptcut import __version__
from sculptcut.ui import MainWindow
from sculptcut.ui_language import install_language
from sculptcut.update_dialog import UpdateChecker, UpdateController
from sculptcut.updates import MAX_RESPONSE_BYTES, REPOSITORY, available_update


def release(version: str = "0.1.2") -> dict[str, object]:
    filename = f"ArtLapse-{version}-Windows-x64.zip"
    return {"tag_name":"v"+version,"draft":False,"prerelease":False,"assets":[
        {"name":f"ArtLapse-{version}-ThirdParty-Sources.zip","state":"uploaded","size":99999,
         "browser_download_url":"https://github.com/source-archive.zip"},
        {"name":filename,"state":"uploaded","size":10000,"browser_download_url":
            f"https://github.com/{REPOSITORY}/releases/download/v{version}/{filename}"},
    ]}


@pytest.mark.parametrize(("current","latest","newer"), [
    ("0.1.1","0.1.2",True),("0.1.9","0.1.10",True),("0.2.0","1.0.0",True),
    ("0.1.1","0.1.1",False),("0.1.1","0.1.0",False),("1.0.0","0.99.99",False),
])
def test_selects_newer_complete_windows_archive(current: str, latest: str, newer: bool) -> None:
    update = available_update(release(latest),current)
    assert bool(update) == newer
    if update is not None:
        assert update.version == latest
        assert update.download_url.endswith(f"ArtLapse-{latest}-Windows-x64.zip")


@pytest.mark.parametrize(
    "change", ["draft","prerelease","preview_tag","source_only","not_uploaded"],
)
def test_unusable_or_preview_release_is_not_offered(change: str) -> None:
    data = release()
    if change in ("draft","prerelease"):
        data[change] = True
    elif change == "preview_tag":
        data["tag_name"] = "v0.1.2-preview.1"
    else:
        assets = data["assets"]
        assert isinstance(assets,list)
        if change == "source_only":
            data["assets"] = assets[:1]
        else:
            assets[1]["state"] = "new"
    assert available_update(data,"0.1.1") is None


def test_unexpected_download_url_is_never_opened() -> None:
    data = release()
    assets = data["assets"]
    assert isinstance(assets,list)
    assets[1]["browser_download_url"] = "https://unrelated.example/program.zip"
    with pytest.raises(ValueError,match="download URL"):
        available_update(data,"0.1.1")


@dataclass
class HttpState:
    body: bytes = field(default_factory=lambda:json.dumps(release()).encode())
    status: int = 200
    delay: float = 0
    requests: list[dict[str,str]] = field(default_factory=list)
    endpoint: str = ""


@pytest.fixture
def http_release() -> Iterator[HttpState]:
    state = HttpState()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            state.requests.append({"method":self.command,"path":self.path,
                "user_agent":self.headers.get("User-Agent",""),
                "body_length":self.headers.get("Content-Length","0"),
                "authorization":self.headers.get("Authorization","")})
            Event().wait(state.delay)
            self.send_response(state.status)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(state.body)))
            if state.status == 302:
                self.send_header("Location",state.endpoint+"/unexpected")
            self.end_headers()
            try:
                self.wfile.write(state.body)
            except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):
                pass

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1",0),Handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever,daemon=True)
    thread.start()
    state.endpoint = f"http://127.0.0.1:{server.server_port}/latest"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def settle(checker: UpdateChecker) -> None:
    deadline = monotonic()+3
    while checker.reply is not None and monotonic() < deadline:
        QTest.qWait(10)
    assert checker.reply is None


def checker_for(state: HttpState, *, timeout: int = 2000) -> UpdateChecker:
    return UpdateChecker(endpoint=state.endpoint,current="0.1.1",timeout_ms=timeout,
                          allow_local_http=True)


def test_real_check_is_nonblocking_sends_no_project_data_and_deduplicates(
    http_release: HttpState,
) -> None:
    app = QApplication.instance() or QApplication([])
    http_release.delay = .25
    checker = checker_for(http_release)
    results: list[tuple[object,str]] = []
    checker.completed.connect(lambda value,error:results.append((value,error)))
    ticks = []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda:ticks.append(True))
    timer.start()
    try:
        checker.start()
        checker.start()
        settle(checker)
        assert len(ticks) >= 3
        assert len(results) == 1 and results[0][0] is not None and not results[0][1]
        assert len(http_release.requests) == 1
        request = http_release.requests[0]
        assert request == {"method":"GET","path":"/latest",
                           "user_agent":f"ArtLapse/{checker.current}",
                           "body_length":"0","authorization":""}
    finally:
        timer.stop()
        checker.shutdown()
        app.processEvents()


@pytest.mark.parametrize(("status","body"), [
    (500,b"offline"),(200,b"not json"),(302,b""),(200,b"x"*(MAX_RESPONSE_BYTES+100)),
], ids=["server", "json", "redirect", "oversized"])
def test_real_network_failures_do_not_report_up_to_date(
    http_release: HttpState, status: int, body: bytes,
) -> None:
    app = QApplication.instance() or QApplication([])
    http_release.status,http_release.body = status,body
    checker = checker_for(http_release)
    results: list[tuple[object,str]] = []
    checker.completed.connect(lambda value,error:results.append((value,error)))
    try:
        checker.start()
        settle(checker)
        assert len(results) == 1 and results[0][0] is None and results[0][1]
        assert len(http_release.requests) == 1
    finally:
        checker.shutdown()
        app.processEvents()


def test_malformed_release_does_not_report_up_to_date(http_release: HttpState) -> None:
    app = QApplication.instance() or QApplication([])
    http_release.body = b'{}'
    checker = checker_for(http_release)
    results: list[tuple[object, str]] = []
    checker.completed.connect(lambda value, error: results.append((value, error)))
    try:
        checker.start()
        settle(checker)
        assert len(results) == 1 and results[0][0] is None and results[0][1]
    finally:
        checker.shutdown()
        app.processEvents()


def test_timeout_and_shutdown_cancel_pending_requests(http_release: HttpState) -> None:
    app = QApplication.instance() or QApplication([])
    http_release.delay = .6
    checker = checker_for(http_release,timeout=150)
    results: list[tuple[object,str]] = []
    checker.completed.connect(lambda value,error:results.append((value,error)))
    checker.start()
    settle(checker)
    assert len(results) == 1 and results[0][1]
    checker.shutdown()
    other = checker_for(http_release)
    other.completed.connect(lambda value,error:pytest.fail("Closed check emitted a result"))
    other.start()
    QTest.qWait(50)
    other.shutdown()
    QTest.qWait(700)
    assert other.reply is None
    app.processEvents()


def test_startup_offer_is_compact_nonmodal_and_download_requires_a_click(
    http_release: HttpState, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    install_language("en")
    window = QWidget()
    window.resize(800,600)
    button = QPushButton(window)
    checker = checker_for(http_release)
    controller = UpdateController(window,button,checker)
    opened: list[str] = []
    monkeypatch.setattr(QDesktopServices,"openUrl",lambda url:opened.append(url.toString()) or True)
    window.show()
    try:
        controller.schedule_startup()
        assert not http_release.requests
        QTest.qWait(1700)
        settle(checker)
        dialog = controller.dialog
        assert dialog is not None and dialog.isVisible()
        assert dialog.width() == 380 and dialog.height() < 210
        assert dialog.windowModality() == Qt.WindowModality.NonModal
        assert "0.1.2" in dialog.message.text() and "Доступна" not in dialog.message.text()
        assert not opened
        assert dialog.download_button is not None
        QTest.mouseClick(dialog.download_button,Qt.MouseButton.LeftButton)
        assert len(opened) == 1 and opened[0].endswith("ArtLapse-0.1.2-Windows-x64.zip")
        assert controller.dialog is None
    finally:
        controller.shutdown()
        window.close()
        app.processEvents()
        install_language("ru")


def test_auto_failures_are_silent_manual_checks_explain_result(http_release: HttpState) -> None:
    app = QApplication.instance() or QApplication([])
    window = QWidget()
    button = QPushButton(window)
    checker = checker_for(http_release)
    controller = UpdateController(window,button,checker)
    try:
        http_release.status = 500
        controller.check(manual=False)
        settle(checker)
        assert controller.dialog is None
        button.click()
        settle(checker)
        assert controller.dialog is not None
        assert "Не удалось" in controller.dialog.message.text()
        controller.dialog.reject()
        http_release.status = 200
        http_release.body = json.dumps(release("0.1.0")).encode()
        button.click()
        settle(checker)
        assert controller.dialog is not None
        assert "Новых версий" in controller.dialog.message.text()
        assert controller.dialog.download_button is None
    finally:
        controller.shutdown()
        window.close()
        app.processEvents()


def test_main_window_button_and_close_cancel_update_work() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.show()
        app.processEvents()
        assert window.update_button.isVisible() and not window.update_button.icon().isNull()
        assert window.update_button.width() == 36
        assert window.updates.checker.current == __version__
        window.updates.schedule_startup()
        assert window.updates.startup.isActive()
    finally:
        window.close()
        app.processEvents()
    assert window.updates.closed and not window.updates.startup.isActive()


def test_network_endpoint_cannot_be_replaced_with_an_unrelated_service() -> None:
    app = QApplication.instance() or QApplication([])
    with pytest.raises(ValueError):
        UpdateChecker(endpoint="https://unrelated.example/latest")
    app.processEvents()
