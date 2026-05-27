"""Тесты для `services/quiet_hours` — тихий режим админ-чата.

Pure-функция `_is_in_window` тестируется на edge cases (полночь,
пустое окно, обе границы). Async `is_quiet_hours_now` — на сценариях
«флаг выключен», «БД упала», «час попадает / не попадает в окно».
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from aemr_bot.services.quiet_hours import _is_in_window, is_quiet_hours_now


class TestIsInWindow:
    """Pure helper — окно может пересекать полночь."""

    def test_window_in_same_day_inside(self) -> None:
        # окно 9:00–18:00, сейчас 12 → внутри
        assert _is_in_window(12, 9, 18) is True

    def test_window_in_same_day_outside(self) -> None:
        # окно 9:00–18:00, сейчас 8 → вне (до)
        assert _is_in_window(8, 9, 18) is False
        # сейчас 18 → вне (граница исключена)
        assert _is_in_window(18, 9, 18) is False

    def test_window_in_same_day_inclusive_start(self) -> None:
        # окно 9:00–18:00, сейчас ровно 9 → внутри (начало включительно)
        assert _is_in_window(9, 9, 18) is True

    def test_window_crosses_midnight_evening(self) -> None:
        # default тихий режим 18:00–09:00, сейчас 22 → внутри
        assert _is_in_window(22, 18, 9) is True

    def test_window_crosses_midnight_night(self) -> None:
        # default 18:00–09:00, сейчас 2 ночи → внутри
        assert _is_in_window(2, 18, 9) is True

    def test_window_crosses_midnight_morning_outside(self) -> None:
        # default 18:00–09:00, сейчас 10 утра → вне
        assert _is_in_window(10, 18, 9) is False

    def test_empty_window_never_active(self) -> None:
        # start == end → пустое окно → False всегда
        for hour in range(24):
            assert _is_in_window(hour, 12, 12) is False


class TestIsQuietHoursNow:
    """Async — читает из settings_store. Best-effort при ошибках."""

    @pytest.mark.asyncio
    async def test_disabled_returns_false(self) -> None:
        """Если `admin_quiet_hours_enabled=False` — всегда False
        независимо от часа."""
        with patch(
            "aemr_bot.services.quiet_hours.settings_store.get",
            AsyncMock(side_effect=lambda s, key: {
                "admin_quiet_hours_enabled": False,
                "admin_quiet_hours_start": 18,
                "admin_quiet_hours_end": 9,
            }[key]),
        ):
            assert await is_quiet_hours_now(SimpleNamespace()) is False

    @pytest.mark.asyncio
    async def test_db_error_returns_false(self) -> None:
        """Best-effort: при ошибке БД не подавляем (лучше шум, чем
        потеря критичного уведомления)."""
        with patch(
            "aemr_bot.services.quiet_hours.settings_store.get",
            AsyncMock(side_effect=RuntimeError("db down")),
        ):
            assert await is_quiet_hours_now(SimpleNamespace()) is False

    @pytest.mark.asyncio
    async def test_invalid_int_returns_false(self) -> None:
        """Если start/end не int (mis-set админом) — не подавляем."""
        with patch(
            "aemr_bot.services.quiet_hours.settings_store.get",
            AsyncMock(side_effect=lambda s, key: {
                "admin_quiet_hours_enabled": True,
                "admin_quiet_hours_start": "18",  # str вместо int
                "admin_quiet_hours_end": 9,
            }[key]),
        ):
            assert await is_quiet_hours_now(SimpleNamespace()) is False
