# Разметка коротких реальных записей

Храните частные видео и их разметку вне Git (например, в artifacts/).
Оригиналы не загружаются в сеть и не копируются в набор синтетики.
Все интервалы — полуоткрытые микросекунды относительно начала источника.

```json
{
  "schema_version": 1,
  "clips": [
    {
      "id": "private-example",
      "path": "relative/or/absolute/video.mp4",
      "duration_us": 10000000,
      "annotation_origin": "human",
      "reviewed": false,
      "segments": [
        {"start_us": 0, "end_us": 10000000, "kind": "UNKNOWN"}
      ]
    }
  ]
}
```

Замените UNKNOWN фактической разметкой STATIC/CAMERA/SCULPT/INTERFACE/MIXED/
PROCESSING/FINAL_VIEW/UNKNOWN и выставьте reviewed=true только после просмотра.
Черновую разметку, подготовленную по отдельным кадрам или автоматическим
подсказкам, помечайте `annotation_origin="draft"`, `reviewed=false`. После
покадровой проверки человеком измените origin на `human` и reviewed на true.
Покройте всю шкалу без пересечений и разрывов. При сомнении используйте UNKNOWN.
Метрики без проверенной разметки не вычисляются. `annotation_origin=synthetic`
нельзя выдавать за оценку реальных записей ZBrush.

В отчёте `sculpt_deleted_*` считает только явные решения DROP.
`sculpt_source_omitted_*` считает также интервалы лепки, которые не попали в
выбранные эпизоды. `sculpt_source_covered_*` показывает долю исходного времени
лепки внутри KEEP/SPEED. Это охват исходной шкалы, а не доля каждого кадра в MP4;
отдельные снимки MONTAGE не считаются непрерывным показом процесса.

Генератор: `python -m sculptcut fixtures artifacts/fixtures`.
Он создаёт static/pan/zoom/local/UI/mixed/loop/VFR/rotation/audio/Unicode
и описывает порядок нескольких исходников. Повторный вызов не перезаписывает файлы.

Для размеченного ZBrush источника запускайте оценку нужного режима отдельно:
`sculptcut-cli.exe evaluate annotations.json report-timelapse --preset timelapse
--target-seconds 600` или `sculptcut-cli.exe evaluate annotations.json report-shorts
--preset shorts`. Для Shorts используется реальная минутная цель текущего
пресета; отчёт не смешивает её с обычным таймлапсом.
