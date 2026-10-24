# Подготовка исходников к GitHub

## ArtLapse 0.1.1 — 2026-10-24

Current source and complete Windows portable release: update checks and restored
Shorts episode framing/scale fixes. The publication checkout passed43targeted
tests in30.92seconds, Ruff and mypy122. Real source/frozen Qt HTTP checks,
RU/EN playback and current screenshots/GIFs were verified. The source release
contains no personal projects, preferences or original recordings.
The Windows bundle retains the same151dependency binaries as0.1.0 and uses
the existing published component source kit. See [release notes](releases/0.1.1.md)
and [Windows validation](launch/RELEASE_CHECKLIST.md).

## Готовый Windows-архив — 2026-10-24

Опубликованная версия исходников дополнена полным portable Windows-комплектом0.1.0. См. [проверки состава и запуска](launch/RELEASE_CHECKLIST.md). Для запуска пользователю нужен только ArtLapse-0.1.0-Windows-x64.zip; Python/FFmpeg устанавливать не требуется. Ниже — протокол проверки исходников предыдущего шага.

## Обновление — 2026-10-24

Актуальный код включает совместные правки подрезки и эпизодов, экспорт с
несохранёнными вырезками, сохранение длительности во всех восьми режимах,
удаление готовых версий, компактное окно эпизодов и однострочные шаблоны.

Проверена именно обновлённая копия исходников:

- 51 профильный тест: PASS за31.70с, без пропусков. Проверены шаблоны,
  отмена создания, эпизоды, компоновка подрезки, удаление версий, anchored
  panels, каталог перевода и настоящий Create с несохранёнными вырезками.
- Ruff всего репозитория: PASS. Строгий mypy: PASS119исходных модулей.
- Короткий экспорт в профильном тесте проверен ffprobe и декодированием.
- Реальное окно и воспроизведение демонстрационного проекта EN/RU: PASS.
- Обновлены снимки рабочего окна, подрезки, рамки, эпизодов и шаблонов EN/RU;
  GIF записаны из работающего плеера, по32кадра, ffprobe/full decode PASS.
- Состав исходников, ignore-правила, ссылки обеих страниц и шаблоны ключей
  проверены. Это ограниченная проверка, не полный аудит безопасности.

Полный pytest повторно не запускался: по просьбе владельца выполняются
только нужные проверки. Эта запись не заменяет прежние результаты полного
прогона и не приписывает их новому запуску. Номер пакета остаётся0.1.0.

В Git входят код, тесты, документация, снимки, лицензии и скрипты сборки.
Исходные видео, проекты, музыка, настройки, журналы, EXE и ZIP не включены.
Настоящие демонстрационные записи и настройки съёмки изолированы от рабочих.

Повторная полная проверка при необходимости:

```powershell
.\.venv\Scripts\python.exe scripts/validate.py
```

GitHub Actions выполняет Ruff, mypy и быстрые тесты без FFmpeg. Результат
удалённого запуска смотрите во вкладке Actions; локальный профильный прогон
и CI проверяют разный объём. Бинарная раздача ведётся отдельно:
[RELEASE_CHECKLIST.md](launch/RELEASE_CHECKLIST.md).
