"""Производственный календарь РФ для подавления SLA-напоминаний в нерабочие дни.

Источник: ТК РФ ст. 112 (государственные нерабочие праздники) плюс
ежегодные постановления Правительства РФ о переносе выходных. Конкретные
даты живут в `seed/holidays.json` — раз в год администратор обновляет
файл и пересобирает контейнер.

Используется только в `services/cron.py` для двух reminder-jobs.
Pulse-job'ы и retention-job'ы намеренно НЕ привязаны к календарю:
«бот жив» нужно знать в любой день, а ретенция ПДн по 152-ФЗ обязана
работать в выходные тоже (срок 30 дней не делает каникул).
"""

from __future__ import annotations

import json
import logging
from datetime import date
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

HOLIDAYS_PATH = Path("/app/seed/holidays.json")


@lru_cache(maxsize=1)
def _load_holidays() -> frozenset[date]:
    """Читает seed/holidays.json и возвращает множество дат-выходных.

    Кэш на жизнь процесса: апдейт файла требует рестарта контейнера —
    это тот же путь, что для всех остальных seed-настроек, и
    предсказуемее, чем file-watcher.
    """
    if not HOLIDAYS_PATH.exists():
        log.warning(
            "calendar_ru: holidays.json not found at %s — fallback to "
            "weekend-only schedule. Update seed/holidays.json yearly.",
            HOLIDAYS_PATH,
        )
        return frozenset()
    try:
        raw = json.loads(HOLIDAYS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.exception("calendar_ru: failed to load holidays.json — fallback to empty")
        return frozenset()
    items: list[str] = []
    if isinstance(raw, dict):
        for key, value in raw.items():
            # `_comment` / `_source` — служебные поля, пропускаем.
            if key.startswith("_") or not isinstance(value, list):
                continue
            items.extend(value)
    elif isinstance(raw, list):
        items = raw
    parsed: set[date] = set()
    for entry in items:
        try:
            parsed.add(date.fromisoformat(entry))
        except ValueError:
            log.warning("calendar_ru: bad date in holidays.json: %r — skipped", entry)
    return frozenset(parsed)


def is_holiday(d: date) -> bool:
    """Праздник по производственному календарю РФ."""
    return d in _load_holidays()


def is_workday(d: date) -> bool:
    """True, если оператор должен работать: пн–сб и не праздник.

    Воскресенье — всегда выходной (расписание pulse-sunday уведомляет
    об этом отдельно). Праздники — даты из `seed/holidays.json`,
    включая переносы (8 марта на пн, 9 мая на пн и т.п.).
    """
    return d.weekday() != 6 and not is_holiday(d)
