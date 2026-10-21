"""Russian presentation strings; persisted identifiers remain language independent."""

import re

from sculptcut.i18n import tr

LABELS = {
    "balanced": "Сбалансированный",
    "calm": "Спокойный",
    "dynamic": "Динамичный",
    "full": "Весь кадр",
    "canvas": "Область модели",
    "viewport": "Окно вьюпорта",
    "hybrid": "Автокадрирование",
    "loop": "Повторять",
    "trim": "Обрезать / дополнить тишиной",
    "KEEP": "Сохранить",
    "DROP": "Удалить",
    "SET_SPEED": "Изменить скорость",
    "RETAIN": "Оставить с ускорением",
    "FORCE_MONTAGE": "Ключевые состояния",
    "LOCK_SEGMENT": "Зафиксировать",
    "STATIC": "Простой",
    "CAMERA": "Движение камеры",
    "SCULPT": "Лепка",
    "INTERFACE": "Работа с интерфейсом",
    "MIXED": "Смешанная активность",
    "PROCESSING": "Ожидание обработки",
    "FINAL_VIEW": "Финальный вид",
    "UNKNOWN": "Неопределённая активность",
}

STAGES = {
    "Reading video metadata and preparing project": "Чтение сведений о видео и подготовка проекта",
    "Checking timestamps: ": "Проверка временных меток: ",
    "Creating analysis proxy: ": "Создание уменьшенной копии: ",
    "Creating and checking analysis proxy: ": "Создание и проверка уменьшенной копии: ",
    "Measuring frame changes": "Анализ изменений кадров",
    "Classifying motion and building the edit plan": "Анализ движения и построение плана монтажа",
    "Creating review cards": "Подготовка карточек проверки",
    "Updating the edit plan": "Обновление плана монтажа",
    "Analyzing camera motion and classifying activity": "Анализ движения и действий · ЦП",
    "Checking selected clips for blank screens": "Проверка выбранных фрагментов · ЦП",
    "Removing zoom-only movements and static views": "Удаление зума и неподвижных кадров · ЦП",
    "Selecting moments for review": "Выбор спорных моментов для проверки",
    "Rendering preview": "Создание предпросмотра",
    "Rendering MP4 from originals": "Экспорт MP4 из исходных видео",
}

ERRORS = {
    "Too many menu frames for flash repair; rebuild the edit plan": (
        "В монтаже слишком много кадров с меню. "
        "Экспорт остановлен: требуется уточнить отбор фрагментов."
    ),
    "No sculpting footage found in strict selection": (
        "Не удалось уверенно найти лепку. Отключите «Только лепка» или добавьте другое видео."
    ),
    "Music folder does not exist": "Папка с музыкой не найдена. Выберите доступную папку.",
    "Music folder contains no supported audio files": "В папке нет поддерживаемых аудиофайлов.",
    "Music folder changed during assembly": "Содержимое папки музыки изменилось. Повторите сборку.",
    "Too many short music tracks": "Для такой длительности нужны более длинные музыкальные треки.",
    "Rendered duration differs from plan": (
        "Длительность MP4 не совпала с планом. Экспорт не опубликован; подробности в журнале."
    ),
    "Rendered frame count differs from plan": (
        "Число кадров MP4 не совпало с планом. Экспорт не опубликован; подробности в журнале."
    ),
    "Proxy ended before requested preview time": (
        "Копия для предпросмотра обрывается раньше нужного момента. Пересоздайте proxy."
    ),
    "Choose a project folder and add at least one video.": (
        "Выберите папку проекта и добавьте видео."
    ),
    "For different source files, choose a new empty project folder": (
        "Для другого набора видео выберите новую пустую папку проекта."
    ),
    "Select a source for ROI": "Выберите видео для настройки области модели.",
    "Add a source before setting crop": "Добавьте видео, чтобы задать рамку таймлапса.",
    "Cannot open source video for crop selection": "Не удалось открыть видео для настройки рамки.",
    "Cannot read source frame for crop selection": "Не удалось прочитать кадр для настройки рамки.",
    "Select a review range first": "Сначала выберите фрагмент для проверки.",
    "Analyze the project to create review cards": (
        "Запустите анализ, чтобы создать карточки проверки."
    ),
    "Project directory must be empty": "Для нового проекта нужна пустая папка.",
    "ROI extends beyond image": "Область модели выходит за границы кадра.",
    "Overrides overlap or reference invalid source ranges": (
        "Ручные решения пересекаются или выходят за границы видео."
    ),
    "Canvas mode requires an explicit ROI for each source": (
        "Задайте область модели для каждого видео перед кадрированием."
    ),
    "Analysis incomplete; run analyze before planning": (
        "Анализ не завершён. Нажмите «Анализ и план»."
    ),
    "Output must be a new .mp4 path": (
        "Выберите новое имя файла MP4: существующий файл не перезаписывается."
    ),
    "Music file has no audio stream": "В выбранном файле музыки нет звуковой дорожки.",
    "Music changed; rebuild the plan": "Файл музыки изменился. Постройте план заново.",
    "Overrides drop all footage": (
        "Ручные решения удаляют всё видео. Сохраните хотя бы один фрагмент."
    ),
    "После вырезки недостаточно исходного материала": (
        "После вырезки не хватает оставшегося исходного материала, чтобы достроить "
        "полный шортс без повторов или искусственного замедления."
    ),
    "У выбранного шортса нет сохранённого монтажного плана": (
        "Этот старый шортс можно укоротить, но достроить до полной минуты нельзя: "
        "у него нет сохранённого монтажного плана."
    ),
    "Заставку шортса нельзя вырезать этим инструментом": (
        "Заставку нельзя удалить как участок шортса. Выделите фрагмент после заставки "
        "или смените её отдельной кнопкой."
    ),
    "Этот старый монтажный план нельзя безопасно достроить": (
        "Этот старый шортс нельзя безопасно достроить после вырезки. Соберите его "
        "заново в текущей версии приложения."
    ),
    "No video stream": "В файле не найдена видеодорожка.",
    "Unsupported project schema; no legacy project migration is defined": (
        "Эта версия формата проекта не поддерживается."
    ),
    "Non-increasing presentation timestamps; repair/reimport required": (
        "Временные метки видео нарушены. Нужен исправленный файл для повторного импорта."
    ),
    "Proxy duration mismatch; timestamp discontinuity requires review": (
        "Длительность уменьшенной копии не совпала с исходником. Проверьте временные метки видео."
    ),
}


def stage_text(message: str) -> str:
    for key, translated in STAGES.items():
        if message == key or (key.endswith(": ") and message.startswith(key)):
            return tr(translated) + message[len(key) :]
    return tr(message)


def error_text(message: str) -> str:
    # Preserve actionable Russian validation messages instead of hiding their cause.
    if "cancelled" in message.lower():
        return tr(
            "Обработка отменена. Сохранённые результаты можно использовать при повторном запуске."
        )
    for key, translated in ERRORS.items():
        if key in message:
            return tr(translated)
    for key, translated in {
        "Source does not exist: ": "Исходное видео не найдено: ",
        "Music file does not exist: ": "Файл музыки не найден: ",
        "Unknown or zero duration: ": "Не удалось определить длительность видео: ",
    }.items():
        if key in message:
            return tr(translated) + message.split(key, 1)[1]
    if "ffmpeg" in message.lower() or "ffprobe" in message.lower():
        if "not found" in message or "does not exist" in message:
            return tr("Не найден FFmpeg или ffprobe. Проверьте установку и пути к инструментам.")
    if "PermissionError" in message or "[Errno 13]" in message:
        return tr("Нет доступа к файлу или папке. Выберите доступную папку и проверьте права.")
    if "[Errno 28]" in message:
        return tr("Недостаточно места на диске. Освободите место и повторите обработку.")
    if "validation error" in message:
        return tr("Некорректные параметры проекта. Проверьте значения и область модели.")
    if re.search(r"[А-Яа-яЁё]", message.splitlines()[0] if message else ""):
        line = message.splitlines()[0]
        prefix, separator, body = line.partition(": ")
        if separator and re.fullmatch(r"[A-Za-z_][\w.]*", prefix):
            line = body
        translated = tr(line)
        for label in ("Исходное видео", "Музыка", "Заставка", "Видео-презентация"):
            if line.startswith(label + ":"):
                return tr(label) + translated[len(label):]
        return translated
    return tr("Не удалось выполнить операцию. Нажмите «Подробности ошибки».")
