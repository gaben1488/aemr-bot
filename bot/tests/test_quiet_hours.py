"""Тесты для `services/quiet_hours` — тихий режим админ-чата.

Pure-функция `_is_in_window` — edge cases (полночь, пустое окно).
Sync `is_quiet_hours_now()` — sync read из in-memory cache.
Async `refresh_cache_from_db(session)` — обновление кэша из settings.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from aemr_bot.services.quiet_hours import (
    _cache,
    _is_in_window,
    is_quiet_hours_now,
    refresh_cache_from_db,
    reset_cache_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_cache_each_test():
    """Изоляция: cache в default state до и после каждого теста."""
    reset_cache_for_tests()
    yield
    reset_cache_for_tests()


class TestIsInWindow:
    """Pure helper — окно может пересекать полночь."""

    def test_window_in_same_day_inside(self) -> None:
        assert _is_in_window(12, 9, 18) is True

    def test_window_in_same_day_outside(self) -> None:
        assert _is_in_window(8, 9, 18) is False
        assert _is_in_window(18, 9, 18) is False  # exclusive end

    def test_window_in_same_day_inclusive_start(self) -> None:
        assert _is_in_window(9, 9, 18) is True

    def test_window_crosses_midnight_evening(self) -> None:
        assert _is_in_window(22, 18, 9) is True

    def test_window_crosses_midnight_night(self) -> None:
        assert _is_in_window(2, 18, 9) is True

    def test_window_crosses_midnight_morning_outside(self) -> None:
        assert _is_in_window(10, 18, 9) is False

    def test_empty_window_never_active(self) -> None:
        for hour in range(24):
            assert _is_in_window(hour, 12, 12) is False


class TestIsQuietHoursNowSync:
    """Sync — читает только cached values."""

    def test_default_cache_returns_false(self) -> None:
        """До любого refresh: cache disabled → False всегда."""
        assert is_quiet_hours_now() is False

    def test_cache_disabled_returns_false(self) -> None:
        _cache["enabled"] = False
        _cache["start"] = 0
        _cache["end"] = 24
        assert is_quiet_hours_now() is False

    def test_invalid_int_returns_false(self) -> None:
        """Если cache содержит non-int — не подавляем (best-effort)."""
        _cache["enabled"] = True
        _cache["start"] = "18"  # str вместо int
        _cache["end"] = 9
        assert is_quiet_hours_now() is False

    def test_enabled_returns_bool(self) -> None:
        """Включённое окно 0–23 → возвращает bool (зависит от текущего часа)."""
        _cache["enabled"] = True
        _cache["start"] = 0
        _cache["end"] = 23
        result = is_quiet_hours_now()
        assert isinstance(result, bool)


class TestRefreshCacheFromDb:
    """Async refresh — best-effort при ошибках БД."""

    @pytest.mark.asyncio
    async def test_disabled_keeps_default(self) -> None:
        with patch(
            "aemr_bot.services.quiet_hours.settings_store.get",
            AsyncMock(side_effect=lambda s, key: {
                "admin_quiet_hours_enabled": False,
                "admin_quiet_hours_start": 18,
                "admin_quiet_hours_end": 9,
            }[key]),
        ):
            await refresh_cache_from_db(SimpleNamespace())
        assert _cache["enabled"] is False
        assert _cache["start"] == 18
        assert _cache["end"] == 9

    @pytest.mark.asyncio
    async def test_enabled_updates_cache(self) -> None:
        with patch(
            "aemr_bot.services.quiet_hours.settings_store.get",
            AsyncMock(side_effect=lambda s, key: {
                "admin_quiet_hours_enabled": True,
                "admin_quiet_hours_start": 20,
                "admin_quiet_hours_end": 8,
            }[key]),
        ):
            await refresh_cache_from_db(SimpleNamespace())
        assert _cache["enabled"] is True
        assert _cache["start"] == 20
        assert _cache["end"] == 8

    @pytest.mark.asyncio
    async def test_db_error_keeps_cache_unchanged(self) -> None:
        # Pre-fill cache to verify previous values survive on error
        _cache["enabled"] = True
        _cache["start"] = 22
        _cache["end"] = 7
        with patch(
            "aemr_bot.services.quiet_hours.settings_store.get",
            AsyncMock(side_effect=RuntimeError("db down")),
        ):
            await refresh_cache_from_db(SimpleNamespace())
        assert _cache["enabled"] is True
        assert _cache["start"] == 22
        assert _cache["end"] == 7

    @pytest.mark.asyncio
    async def test_invalid_int_falls_back_to_defaults(self) -> None:
        with patch(
            "aemr_bot.services.quiet_hours.settings_store.get",
            AsyncMock(side_effect=lambda s, key: {
                "admin_quiet_hours_enabled": True,
                "admin_quiet_hours_start": "broken",
                "admin_quiet_hours_end": None,
            }[key]),
        ):
            await refresh_cache_from_db(SimpleNamespace())
        assert _cache["start"] == 18
        assert _cache["end"] == 9
