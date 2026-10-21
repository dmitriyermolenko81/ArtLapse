"""Owner-only setup helper: read bot updates and print group IDs, never print the token."""

import argparse
import getpass
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, help="Save only the discovered group IDs, never the key"
    )
    parser.add_argument(
        "--pause", action="store_true", help="Keep the setup window open on completion"
    )
    parser.add_argument("--gui", action="store_true", help="Use dialogs instead of console input")
    args = parser.parse_args()
    if args.gui:
        from PySide6.QtWidgets import (
            QApplication,
            QDialog,
            QInputDialog,
            QLabel,
            QLineEdit,
            QPushButton,
        )

        from sculptcut.ui_theme import STYLE, show_notice, style_setup_dialog

        app = QApplication.instance() or QApplication([])
        app.setStyleSheet(STYLE)
        dialog = QInputDialog()
        dialog.setWindowTitle("Подключение Telegram — SculptCut")
        dialog.setLabelText(
            "Добавьте @SculptCut_bot в группу и вставьте ключ от BotFather.\n"
            "Ключ используется только для запроса к Telegram и не сохраняется."
        )
        dialog.setTextEchoMode(QLineEdit.EchoMode.Password)
        dialog.setOkButtonText("Продолжить")
        dialog.setCancelButtonText("Отмена")
        dialog.setMinimumWidth(510)
        style_setup_dialog(dialog, "Подключение Telegram")
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
    else:
        print("Создайте бота через @BotFather и добавьте его в группу SculptCut.")
        token = getpass.getpass("Ключ бота (ввод скрыт, никуда не сохраняется): ").strip()

    def notify(message: str) -> None:
        if args.gui:
            show_notice(None, "Подключение Telegram — SculptCut", message)
        else:
            print(message)

    if not token or ":" not in token:
        notify("Нужен ключ, выданный BotFather.")
        return

    def api(method: str) -> dict[str, object]:
        request = Request("https://api.telegram.org/bot" + token + "/" + method,
                          data=b"{}", headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=15) as response:
            data: dict[str, object] = json.loads(response.read(262144))
            if data.get("ok") is not True:
                raise ValueError("Telegram rejected setup request")
            return data

    try:
        identity = api("getMe").get("result")
        username = identity.get("username", "") if isinstance(identity, dict) else ""
        instructions = "В группе напишите /setup@" + str(username)
        if args.gui:
            notify(instructions + "\n\nПосле отправки команды нажмите ОК в этом окне.")
        else:
            print(instructions)
            input("После этого нажмите Enter здесь…")
        updates = api("getUpdates").get("result")
        groups: dict[int, str] = {}
        if isinstance(updates, list):
            for update in updates:
                message = update.get("message", {})
                chat = message.get("chat", {})
                if chat.get("type") in ("group", "supergroup"):
                    groups[chat["id"]] = chat.get("title", "Группа")
        if not groups:
            notify("Группа не найдена. Проверьте, что бот добавлен "
                   "и команда отправлена именно в группе.")
        if groups and args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps({"groups": [{"id": chat_id, "title": title}
                                       for chat_id, title in groups.items()]}, ensure_ascii=False),
                encoding="utf-8",
            )
        if groups:
            notify("\n".join(f"{title}: TELEGRAM_CHAT_ID={chat_id}"
                             for chat_id, title in groups.items()))
    except (OSError, HTTPError, URLError, ValueError):
        notify("Не удалось получить данные. Проверьте ключ бота и подключение к Telegram.")
    finally:
        if args.pause and not args.gui:
            input("Нажмите Enter, чтобы закрыть окно…")


if __name__ == "__main__":
    main()
