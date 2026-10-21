# Сборка Windows x64

Собирайте на Windows с Python 3.10+. Из корня репозитория:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,build]"
.\.venv\Scripts\python.exe -m PyInstaller packaging/SculptCut.spec --noconfirm
```

Результат — **вся папка** `dist/SculptCut`; одного EXE недостаточно.
FFmpeg/ffprobe в обычную onedir-сборку автоматически не добавляются.
Локальный пример публичного конфига обратной связи находится в
`packaging/feedback-config.example.json`; его копирование в
`feedback-config.json` необязательно. Ключей бота в приложении быть не должно.

## Переносной ZIP

Подготовьте отдельную Windows-сборку FFmpeg: каталог должен содержать
`bin/ffmpeg.exe`, `bin/ffprobe.exe`, `LICENSE` и `README.txt`.
Не используйте сборку с `--enable-nonfree`.

```powershell
.\.venv\Scripts\python.exe packaging/portable.py --source dist/SculptCut --ffmpeg-root "C:\Tools\ffmpeg" --destination artifacts/portable/SculptCut --archive dist/SculptCut-Windows-x64.zip
```

Каталог назначения и архив должны быть новыми. Упаковщик создаёт ZIP,
SHA256, `portable.txt`, инструкции RU/EN и копии лицензий FFmpeg.
Используйте чистую сборку без `data` и личных проектов.

Распакуйте ZIP в другую папку и проверьте её:

```powershell
Expand-Archive dist/SculptCut-Windows-x64.zip artifacts/extracted
pwsh -NoProfile -File packaging/Smoke-Windows.ps1 -Bundle "$PWD/artifacts/extracted/SculptCut" -Workspace "$PWD/artifacts/smoke-ru" -Language ru
pwsh -NoProfile -File packaging/Smoke-Windows.ps1 -Bundle "$PWD/artifacts/extracted/SculptCut" -Workspace "$PWD/artifacts/smoke-en" -Language en
```

Smoke очищает PATH, проверяет CLI, настоящий экспорт30 кадров, ffprobe,
полное декодирование, окно и воспроизведение. Для каждого Workspace
нужна новая пустая папка. Проверка на машине разработчика не заменяет
проверку на отдельной чистой Windows.

Прежде чем распространять бинарные зависимости, соблюдайте их лицензии;
уведомления перечислены в `packaging/THIRD_PARTY_NOTICES.md`.
