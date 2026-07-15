"""Расчёт SLA-просрочки по РАБОЧЕМУ времени (а не календарному).

Проблема, которую закрывает модуль: `find_overdue_unanswered` и
cron-напоминалки раньше считали порог просрочки как
`now - sla_hours` по календарным часам. Обращение, поступившее в
пятницу в 20:00, оказывалось «просроченным» уже в субботу вечером
(SLA_RESPONSE_HOURS=4), хотя рабочих часов с момента поступления не
прошло ни одного — оператор физически не мог успеть ответить.

Решение: SLA-часы копятся ТОЛЬКО внутри рабочих окон
[sla_work_start_hour, sla_work_end_hour) рабочих дней
(calendar_ru.is_workday — пн-пт минус праздники РФ из
seed/holidays.json) ЗА ВЫЧЕТОМ обеденного перерыва
[sla_lunch_start_hour, sla_lunch_end_hour), по умолчанию 12:00-13:00.
Обед вычитается, потому что в обед люди не работают (решение
владельца); дефолт синхронизирован с окном cron-напоминалок
(hour="9-11,13-17" — час 12 выпадает). Границы окон и валидация
(обед строго внутри рабочего окна) — в config.py у полей
sla_work_start_hour/sla_work_end_hour/sla_lunch_*.

Naive datetime: явный ValueError. Тихое приведение naive → aware
(например через `.replace(tzinfo=...)`) скрывает баг вызывающего
кода — час, посчитанный от «неизвестно какого» времени, для SLA
хуже, чем упавший тест. Все datetime в проекте и так aware
(DateTime(timezone=True) в моделях, `datetime.now(timezone.utc)`
в сервисах) — naive здесь означает ошибку выше по стеку.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from aemr_bot.config import settings
from aemr_bot.services.calendar_ru import is_workday

_TZ = ZoneInfo(settings.timezone)


def _require_aware(dt: datetime, *, param_name: str) -> None:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(
            f"sla.{param_name} требует aware datetime (с tzinfo), получено "
            f"naive: {dt!r}. Приведите к UTC или локальной зоне явно "
            f"перед вызовом — молчаливое приведение здесь запрещено, "
            f"чтобы не считать SLA от неизвестного времени."
        )


def _workday_window(d: date) -> tuple[datetime, datetime]:
    """Границы рабочего окна дня `d` в локальной таймзоне (aware).

    `end` считаем как полночь дня `d` + sla_work_end_hour часов, а не
    `time(sla_work_end_hour, 0)` — так корректно обрабатывается
    предельное значение 24 (конец рабочего окна ровно в полночь
    следующих суток), которое `datetime.time` не может представить
    напрямую (диапазон time() — [0, 23]).
    """
    midnight = datetime.combine(d, time(0, 0), tzinfo=_TZ)
    start = midnight + timedelta(hours=settings.sla_work_start_hour)
    end = midnight + timedelta(hours=settings.sla_work_end_hour)
    return start, end


def _lunch_window(d: date) -> tuple[datetime, datetime]:
    """Границы обеденного перерыва дня `d` в локальной таймзоне (aware).

    Та же арифметика «полночь + часы», что и в _workday_window, — ради
    единообразия и корректной поддержки предельного значения 24.
    """
    midnight = datetime.combine(d, time(0, 0), tzinfo=_TZ)
    start = midnight + timedelta(hours=settings.sla_lunch_start_hour)
    end = midnight + timedelta(hours=settings.sla_lunch_end_hour)
    return start, end


def business_seconds_between(start: datetime, end: datetime) -> float:
    """Сколько секунд из интервала [start, end] попало в рабочие окна
    рабочих дней (calendar_ru.is_workday) за вычетом обеденного
    перерыва [sla_lunch_start_hour, sla_lunch_end_hour) каждого
    рабочего дня (по умолчанию 12:00-13:00).

    Оба аргумента обязаны быть aware datetime; приводятся к локальной
    таймзоне бота (Asia/Kamchatka по умолчанию) перед расчётом, чтобы
    рабочее окно 09:00-18:00 (минус обед 12:00-13:00) сравнивалось с
    локальным временем, а не UTC.

    Если start >= end — возвращает 0.0 (просрочки нет, обращение из
    будущего или интервал вырожден).

    Реализация — прямая итерация по календарным дням диапазона с
    отсечением (clip) каждого дня по рабочему окну и вычитанием
    пересечения полученного отрезка с обеденным окном того же дня.
    Дней в типичном диапазоне (часы-дни SLA) немного, поэтому
    O(количество дней) более чем достаточно и остаётся понятным для
    проверки/отладки — в отличие от «умной» арифметики без цикла,
    которую пришлось бы отдельно доказывать корректной на границах
    суток/недели/праздников.
    """
    _require_aware(start, param_name="start")
    _require_aware(end, param_name="end")

    start_local = start.astimezone(_TZ)
    end_local = end.astimezone(_TZ)

    if start_local >= end_local:
        return 0.0

    total = 0.0
    day = start_local.date()
    last_day = end_local.date()
    while day <= last_day:
        if is_workday(day):
            win_start, win_end = _workday_window(day)
            # Отсекаем окно дня диапазоном [start_local, end_local].
            clipped_start = max(win_start, start_local)
            clipped_end = min(win_end, end_local)
            if clipped_end > clipped_start:
                total += (clipped_end - clipped_start).total_seconds()
                # Обед не считается рабочим временем: вычитаем
                # пересечение учтённого отрезка с обеденным окном дня.
                lunch_start, lunch_end = _lunch_window(day)
                overlap_start = max(lunch_start, clipped_start)
                overlap_end = min(lunch_end, clipped_end)
                if overlap_end > overlap_start:
                    total -= (overlap_end - overlap_start).total_seconds()
        day = day + timedelta(days=1)
    return total


def is_overdue(created_at: datetime, now: datetime, sla_hours: int) -> bool:
    """True, если с момента `created_at` до `now` накопилось не меньше
    `sla_hours` рабочих часов (см. business_seconds_between).
    """
    return business_seconds_between(created_at, now) >= sla_hours * 3600
