"""Working-time SLA arithmetic shared with operator reminders.

Current reminders use the active DB calendar (environment fallback until an
explicit setting is saved). Callers assessing an appeal pass its saved period
calendar. Legacy periods use the frozen operational baseline and must not be
reported as historically certain. Weekends and snapshotted holidays are excluded,
as is the lunch window. All inputs must be aware datetimes.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aemr_bot.services import work_schedule


def _require_aware(dt: datetime, *, param_name: str) -> None:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(
            f"sla.{param_name} требует aware datetime (с tzinfo), получено "
            f"naive: {dt!r}. Приведите к UTC или локальной зоне явно "
            f"перед вызовом — молчаливое приведение здесь запрещено, "
            f"чтобы не считать SLA от неизвестного времени."
        )


def _workday_window(d: date, schedule: dict[str, Any] | None = None) -> tuple[datetime, datetime]:
    """Границы рабочего окна дня `d` в локальной таймзоне (aware).

    `end` считаем как полночь дня `d` + sla_work_end_hour часов, а не
    `time(sla_work_end_hour, 0)` — так корректно обрабатывается
    предельное значение 24 (конец рабочего окна ровно в полночь
    следующих суток), которое `datetime.time` не может представить
    напрямую (диапазон time() — [0, 23]).
    """
    value = schedule or work_schedule.active()
    midnight = datetime.combine(d, time(0, 0), tzinfo=ZoneInfo(value["timezone"]))
    start = midnight + timedelta(hours=value["start"])
    end = midnight + timedelta(hours=value["end"])
    return start, end


def _lunch_window(d: date, schedule: dict[str, Any] | None = None) -> tuple[datetime, datetime]:
    """Границы обеденного перерыва дня `d` в локальной таймзоне (aware).

    Та же арифметика «полночь + часы», что и в _workday_window, — ради
    единообразия и корректной поддержки предельного значения 24.
    """
    value = schedule or work_schedule.active()
    midnight = datetime.combine(d, time(0, 0), tzinfo=ZoneInfo(value["timezone"]))
    start = midnight + timedelta(hours=value["lunch_start"])
    end = midnight + timedelta(hours=value["lunch_end"])
    return start, end


def business_seconds_between(start: datetime, end: datetime, *,
                             schedule: dict[str, Any] | None = None) -> float:
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

    value = schedule or work_schedule.active()
    work_schedule.validate(value)
    tz = ZoneInfo(value["timezone"])
    start_local = start.astimezone(tz)
    end_local = end.astimezone(tz)

    if start_local >= end_local:
        return 0.0

    total = 0.0
    day = start_local.date()
    last_day = end_local.date()
    while day <= last_day:
        if day.weekday() < 5 and day.isoformat() not in value["holidays"]:
            win_start, win_end = _workday_window(day, value)
            # Отсекаем окно дня диапазоном [start_local, end_local].
            clipped_start = max(win_start, start_local)
            clipped_end = min(win_end, end_local)
            if clipped_end > clipped_start:
                total += (clipped_end - clipped_start).total_seconds()
                # Обед не считается рабочим временем: вычитаем
                # пересечение учтённого отрезка с обеденным окном дня.
                lunch_start, lunch_end = _lunch_window(day, value)
                overlap_start = max(lunch_start, clipped_start)
                overlap_end = min(lunch_end, clipped_end)
                if overlap_end > overlap_start:
                    total -= (overlap_end - overlap_start).total_seconds()
        day = day + timedelta(days=1)
    return total


def is_overdue(created_at: datetime, now: datetime, sla_hours: int, *,
               schedule: dict[str, Any] | None = None) -> bool:
    """True, если с момента `created_at` до `now` накопилось не меньше
    `sla_hours` рабочих часов (см. business_seconds_between).
    """
    return business_seconds_between(created_at, now, schedule=schedule) >= sla_hours * 3600


def is_working_time(now: datetime, *, schedule: dict[str, Any] | None = None) -> bool:
    """Use the same calendar and hours for reminders and SLA calculation."""
    _require_aware(now, param_name="now")
    value = schedule or work_schedule.active()
    work_schedule.validate(value)
    local = now.astimezone(ZoneInfo(value["timezone"]))
    if local.weekday() >= 5 or local.date().isoformat() in value["holidays"]:
        return False
    start, end = _workday_window(local.date(), value)
    lunch_start, lunch_end = _lunch_window(local.date(), value)
    return start <= local < end and not lunch_start <= local < lunch_end
