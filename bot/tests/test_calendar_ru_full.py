"""Тесты на services/calendar_ru — праздники и рабочие дни РФ.

Логика is_workday(): пн-сб + не праздник (воскресенье — всегда выходной).
Файл holidays.json в production живёт в /app/seed/. Локально путь
другой — мокаем _load_holidays напрямую."""
from __future__ import annotations

from datetime import date

import pytest

from aemr_bot.services import calendar_ru


@pytest.fixture
def known_holidays(monkeypatch):
    """Подменяем загрузку holidays на фиксированный набор для теста.

    monkeypatch сам восстанавливает оригинал в teardown — cache_clear
    не нужен (мы не вызываем оригинальную lru-cached функцию)."""
    holidays = frozenset({
        date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 7),
        date(2026, 5, 1), date(2026, 5, 9),
        date(2026, 6, 12),
    })
    monkeypatch.setattr(calendar_ru, "_load_holidays", lambda: holidays)
    return holidays


class TestIsHoliday:
    def test_new_year(self, known_holidays) -> None:
        assert calendar_ru.is_holiday(date(2026, 1, 1))

    def test_may_9_victory_day(self, known_holidays) -> None:
        assert calendar_ru.is_holiday(date(2026, 5, 9))

    def test_regular_day_not_holiday(self, known_holidays) -> None:
        assert not calendar_ru.is_holiday(date(2026, 6, 17))


class TestIsWorkday:
    def test_sunday_never_workday(self, known_holidays) -> None:
        # 17 мая 2026 — воскресенье
        assert not calendar_ru.is_workday(date(2026, 5, 17))

    def test_saturday_is_workday_if_not_holiday(self, known_holidays) -> None:
        # 16 мая 2026 — суббота, не праздник → workday по нашей логике
        # (is_workday: пн-сб + не праздник)
        assert calendar_ru.is_workday(date(2026, 5, 16))

    def test_holiday_not_workday(self, known_holidays) -> None:
        # 9 мая — суббота-праздник
        assert not calendar_ru.is_workday(date(2026, 5, 9))

    def test_normal_weekday(self, known_holidays) -> None:
        # 17 июня — среда обычная
        assert calendar_ru.is_workday(date(2026, 6, 17))

    def test_friday_workday(self, known_holidays) -> None:
        assert calendar_ru.is_workday(date(2026, 6, 19))


class TestEmptyHolidays:
    """Граничный кейс: holidays.json не загрузился — fallback на
    weekend-only расписание (пн-сб все рабочие)."""

    def test_fallback_no_crash(self, monkeypatch) -> None:
        monkeypatch.setattr(calendar_ru, "_load_holidays", lambda: frozenset())
        # 9 мая обычный день в этом fallback
        assert not calendar_ru.is_holiday(date(2026, 5, 9))
        # is_workday работает: суббота 16 мая — рабочая
        assert calendar_ru.is_workday(date(2026, 5, 16))
