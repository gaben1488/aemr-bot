"""Тесты на services/sla — расчёт SLA-просрочки по рабочему времени.

Проблема, которую закрывает модуль: обращение, поступившее в пятницу
вечером, не должно считаться просроченным уже в субботу/воскресенье —
рабочих часов ещё не было. SLA-часы копятся ТОЛЬКО внутри рабочих окон
[sla_work_start_hour, sla_work_end_hour) рабочих дней (пн-пт минус
праздники РФ из seed/holidays.json).

Опорная неделя тестов: пятница 2026-06-19 → понедельник 2026-06-22 —
обычная рабочая неделя без праздников (ближайший праздник 2026-06-12,
государственный день России, позади). Рабочее окно по умолчанию из
config.py: 09:00-18:00 Asia/Kamchatka, без вычета обеда (сознательное
решение — см. комментарий в config.py у sla_work_start_hour/end_hour).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from aemr_bot.services import calendar_ru, sla

TZ = ZoneInfo("Asia/Kamchatka")


@pytest.fixture(autouse=True)
def known_holidays(monkeypatch):
    """Фиксированный набор праздников на 2026 год (подмножество реального
    seed/holidays.json), чтобы тест не зависел от содержимого файла на
    диске и не ловил ложный fallback на "нет holidays.json"."""
    holidays = frozenset({
        date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3), date(2026, 1, 4),
        date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8),
        date(2026, 2, 23),
        date(2026, 3, 8), date(2026, 3, 9),
        date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3),
        date(2026, 5, 9), date(2026, 5, 10), date(2026, 5, 11),
        date(2026, 6, 12),
        date(2026, 11, 4),
    })
    monkeypatch.setattr(calendar_ru, "_load_holidays", lambda: holidays)
    return holidays


def _local(y, m, d, hh, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=TZ)


class TestBusinessSecondsBetween:
    def test_same_business_day_midday(self) -> None:
        # Вторник, 10:00 -> 14:00: всё внутри рабочего окна, 4 часа.
        start = _local(2026, 6, 16, 10, 0)
        end = _local(2026, 6, 16, 14, 0)
        assert sla.business_seconds_between(start, end) == 4 * 3600

    def test_before_work_start_clipped(self) -> None:
        # Начало до открытия (07:00) -> считается только с 09:00.
        start = _local(2026, 6, 16, 7, 0)
        end = _local(2026, 6, 16, 10, 0)
        assert sla.business_seconds_between(start, end) == 1 * 3600

    def test_after_work_end_clipped(self) -> None:
        # Конец после закрытия (22:00) -> считается только до 18:00.
        start = _local(2026, 6, 16, 16, 0)
        end = _local(2026, 6, 16, 22, 0)
        assert sla.business_seconds_between(start, end) == 2 * 3600

    def test_friday_evening_to_monday_before_open_is_zero(self) -> None:
        # Пт 20:00 -> Пн 08:59: суббота/воскресенье не рабочие, в
        # понедельник рабочее окно ещё не открылось -> 0 рабочих секунд.
        start = _local(2026, 6, 19, 20, 0)
        end = _local(2026, 6, 22, 8, 59)
        assert sla.business_seconds_between(start, end) == 0.0

    def test_friday_evening_to_monday_after_open(self) -> None:
        # Пт 20:00 -> Пн 10:00: 1 рабочий час накопился с открытия понедельника.
        start = _local(2026, 6, 19, 20, 0)
        end = _local(2026, 6, 22, 10, 0)
        assert sla.business_seconds_between(start, end) == 1 * 3600

    def test_weekend_creation_no_business_time_until_monday_open(self) -> None:
        # Создано в субботу днём (нерабочий день целиком) -> до открытия
        # понедельника рабочего времени 0.
        start = _local(2026, 6, 20, 12, 0)
        end = _local(2026, 6, 22, 8, 0)
        assert sla.business_seconds_between(start, end) == 0.0

    def test_holiday_is_skipped(self) -> None:
        # 2026-06-12 — праздник (пятница). С четверга 17:00 до понедельника
        # (15-го) 10:00: рабочих часов только четверг 17-18 (1ч) + пн 9-10 (1ч).
        # Пятница-праздник и выходные не считаются.
        start = _local(2026, 6, 11, 17, 0)  # четверг
        end = _local(2026, 6, 15, 10, 0)    # понедельник
        assert sla.business_seconds_between(start, end) == 2 * 3600

    def test_multi_day_spanning_full_business_days(self) -> None:
        # Пн 09:00 -> Ср 09:00: пн (9ч) + вт (9ч) + ср 0ч (совпадает со
        # стартом рабочего окна следующего дня) = 18 часов.
        start = _local(2026, 6, 15, 9, 0)
        end = _local(2026, 6, 17, 9, 0)
        assert sla.business_seconds_between(start, end) == 18 * 3600

    def test_start_after_end_returns_zero(self) -> None:
        start = _local(2026, 6, 16, 14, 0)
        end = _local(2026, 6, 16, 10, 0)
        assert sla.business_seconds_between(start, end) == 0.0

    def test_start_equals_end_returns_zero(self) -> None:
        moment = _local(2026, 6, 16, 12, 0)
        assert sla.business_seconds_between(moment, moment) == 0.0

    def test_midnight_boundary_start_of_day(self) -> None:
        # Началось ровно в полночь рабочего дня -> считается с 09:00 того же дня.
        start = _local(2026, 6, 16, 0, 0)
        end = _local(2026, 6, 16, 9, 30)
        assert sla.business_seconds_between(start, end) == 0.5 * 3600

    def test_midnight_boundary_end_of_range(self) -> None:
        # Диапазон заканчивается ровно в полночь следующих суток ->
        # весь предыдущий рабочий день (9ч) учтён полностью, следующий
        # день ещё не наступил (00:00 не входит в его рабочее окно).
        start = _local(2026, 6, 16, 0, 0)
        end = _local(2026, 6, 17, 0, 0)
        assert sla.business_seconds_between(start, end) == 9 * 3600

    def test_accepts_utc_and_converts_to_local(self) -> None:
        # UTC-aware datetime корректно приводится к Asia/Kamchatka
        # (UTC+12) перед расчётом рабочего окна.
        start_utc = _local(2026, 6, 16, 10, 0).astimezone(timezone.utc)
        end_utc = _local(2026, 6, 16, 14, 0).astimezone(timezone.utc)
        assert sla.business_seconds_between(start_utc, end_utc) == 4 * 3600

    def test_naive_datetime_raises_value_error(self) -> None:
        start = datetime(2026, 6, 16, 10, 0)  # naive
        end = _local(2026, 6, 16, 14, 0)
        with pytest.raises(ValueError, match="aware datetime"):
            sla.business_seconds_between(start, end)

    def test_naive_end_raises_value_error(self) -> None:
        start = _local(2026, 6, 16, 10, 0)
        end = datetime(2026, 6, 16, 14, 0)  # naive
        with pytest.raises(ValueError, match="aware datetime"):
            sla.business_seconds_between(start, end)


class TestIsOverdue:
    def test_friday_evening_not_overdue_at_monday_1259(self) -> None:
        # SLA 4ч. Пт 20:00 -> Пн 12:59: с открытия (09:00 пн) прошло
        # 3ч59м рабочего времени -> ещё не просрочено.
        created = _local(2026, 6, 19, 20, 0)
        now = _local(2026, 6, 22, 12, 59)
        assert sla.is_overdue(created, now, sla_hours=4) is False

    def test_friday_evening_overdue_at_monday_1300(self) -> None:
        # Ровно 4 рабочих часа накопилось (09:00-13:00 пн) -> просрочено.
        created = _local(2026, 6, 19, 20, 0)
        now = _local(2026, 6, 22, 13, 0)
        assert sla.is_overdue(created, now, sla_hours=4) is True

    def test_business_hours_creation_overdue_same_day(self) -> None:
        # Создано в рабочий полдень (вт 11:00), SLA 4ч -> просрочка
        # ровно в 15:00 того же дня (в пределах рабочего окна).
        created = _local(2026, 6, 16, 11, 0)
        just_before = _local(2026, 6, 16, 14, 59)
        exactly_due = _local(2026, 6, 16, 15, 0)
        assert sla.is_overdue(created, just_before, sla_hours=4) is False
        assert sla.is_overdue(created, exactly_due, sla_hours=4) is True

    def test_weekend_creation_not_overdue_until_monday_business_hours(self) -> None:
        created = _local(2026, 6, 20, 12, 0)  # суббота
        still_weekend = _local(2026, 6, 21, 23, 0)  # воскресенье
        monday_before_sla = _local(2026, 6, 22, 12, 59)
        monday_after_sla = _local(2026, 6, 22, 13, 0)
        assert sla.is_overdue(created, still_weekend, sla_hours=4) is False
        assert sla.is_overdue(created, monday_before_sla, sla_hours=4) is False
        assert sla.is_overdue(created, monday_after_sla, sla_hours=4) is True

    def test_naive_now_raises_value_error(self) -> None:
        created = _local(2026, 6, 16, 10, 0)
        now = datetime(2026, 6, 16, 14, 0)  # naive
        with pytest.raises(ValueError, match="aware datetime"):
            sla.is_overdue(created, now, sla_hours=4)
