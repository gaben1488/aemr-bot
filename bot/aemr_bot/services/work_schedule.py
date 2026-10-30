"""Versioned working calendar shared by reminders and first-response periods.

The first activation freezes an operational fallback for legacy rows. It is
not evidence of their historical calendar. New periods snapshot their calendar,
including holidays, so future edits (or seed updates) cannot rewrite elapsed time.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from aemr_bot.config import settings
from aemr_bot.db.models import Setting
from aemr_bot.services import calendar_ru, settings_store

KEY = "sla_work_schedule"
LEGACY_KEY = "__sla_legacy_schedule"
_active: dict[str, Any] | None = None
_legacy: dict[str, Any] | None = None


def validate(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {
        "version", "timezone", "start", "end", "lunch_start", "lunch_end", "holidays",
    }:
        raise ValueError("Расписание должно содержать часы, часовой пояс и календарь версии 1.")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("Неизвестная версия расписания.")
    for name in ("start", "end", "lunch_start", "lunch_end"):
        if type(value[name]) is not int:
            raise ValueError("Часы должны быть целыми числами.")
    if not 0 <= value["start"] <= value["lunch_start"] < value["lunch_end"] <= value["end"] <= 24:
        raise ValueError("Нужно: 0 ≤ начало работы ≤ начало обеда < конец обеда ≤ конец работы ≤ 24.")
    try:
        ZoneInfo(value["timezone"])
    except (ZoneInfoNotFoundError, TypeError, ValueError) as exc:
        raise ValueError("Неизвестный часовой пояс.") from exc
    holidays = value["holidays"]
    if not isinstance(holidays, list) or len(holidays) > 10000:
        raise ValueError("Неверный список праздничных дат.")
    try:
        for day in holidays:
            if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
                raise ValueError("Неверная праздничная дата.")
    except (TypeError, ValueError) as exc:
        raise ValueError("Праздничные даты должны иметь формат ГГГГ-ММ-ДД.") from exc


def environment() -> dict[str, Any]:
    return {"version": 1, "timezone": settings.timezone,
            "start": settings.sla_work_start_hour, "end": settings.sla_work_end_hour,
            "lunch_start": settings.sla_lunch_start_hour,
            "lunch_end": settings.sla_lunch_end_hour,
            "holidays": sorted(d.isoformat() for d in calendar_ru._load_holidays())}


def active() -> dict[str, Any]:
    return deepcopy(_active if _active is not None else environment())


def for_appeal(appeal: Any) -> dict[str, Any]:
    saved = getattr(appeal, "sla_schedule_at_start", None)
    if saved is not None:
        validate(saved)
        return deepcopy(saved)
    return deepcopy(_legacy if _legacy is not None else environment())


async def snapshot(session: AsyncSession) -> dict[str, Any]:
    value = await settings_store.get(session, KEY)
    if value is None:
        return environment()
    validate(value)  # Corrupt saved calendars must not silently become another one.
    return deepcopy(value)


async def initialize(session: AsyncSession) -> None:
    """Call once at startup in a committed transaction; never overwrite values."""
    await session.execute(insert(Setting).values(key=LEGACY_KEY, value=environment())
                          .on_conflict_do_nothing(index_elements=[Setting.key]))
    # Validate before startup accepts traffic. refresh after commit publishes cache.
    await snapshot(session)
    validate(await settings_store.get(session, LEGACY_KEY))


async def refresh(session: AsyncSession) -> None:
    """Publish committed DB settings; cron calls before each working-time gate."""
    global _active, _legacy
    current = await snapshot(session)
    legacy = await settings_store.get(session, LEGACY_KEY)
    if legacy is not None:
        validate(legacy)
    _active = current
    _legacy = deepcopy(legacy)
