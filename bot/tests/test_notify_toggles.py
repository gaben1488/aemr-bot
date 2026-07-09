"""Тесты для `services/notify_toggles` — модульные тумблеры служебных
уведомлений в админ-чат.

По образцу `tests/test_quiet_hours.py`: sync read из кэша, async
refresh best-effort при ошибках БД, default True до первого refresh
(в отличие от quiet_hours, где default False).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from aemr_bot.services.notify_toggles import (
    TOGGLE_KEYS,
    _cache,
    is_enabled,
    refresh_cache_from_db,
    reset_cache_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_cache_each_test():
    """Изоляция: cache в default state (все True) до и после теста."""
    reset_cache_for_tests()
    yield
    reset_cache_for_tests()


class TestToggleKeys:
    def test_six_keys_defined(self) -> None:
        assert len(TOGGLE_KEYS) == 6
        assert TOGGLE_KEYS == (
            "admin_notify_pulse",
            "admin_notify_consent",
            "admin_notify_subscriptions",
            "admin_notify_open_reminder",
            "admin_notify_overdue_reminder",
            "admin_notify_monthly_stats",
        )


class TestIsEnabledSync:
    def test_default_cache_returns_true_for_all(self) -> None:
        """До любого refresh: cache по умолчанию True — лучше шум, чем
        потерянный алёрт."""
        for key in TOGGLE_KEYS:
            assert is_enabled(key) is True

    def test_cache_false_returns_false(self) -> None:
        _cache["admin_notify_pulse"] = False
        assert is_enabled("admin_notify_pulse") is False
        # Остальные не затронуты.
        assert is_enabled("admin_notify_consent") is True

    def test_unknown_key_returns_true(self) -> None:
        """Неизвестный ключ трактуется как включённый — тумблер должен
        явно выключать, а не молча глушить всё незнакомое."""
        assert is_enabled("admin_notify_does_not_exist") is True


class TestRefreshCacheFromDb:
    @pytest.mark.asyncio
    async def test_all_true_from_db(self) -> None:
        with patch(
            "aemr_bot.services.notify_toggles.settings_store.get",
            AsyncMock(side_effect=lambda s, key: True),
        ):
            await refresh_cache_from_db(SimpleNamespace())
        for key in TOGGLE_KEYS:
            assert is_enabled(key) is True

    @pytest.mark.asyncio
    async def test_mixed_values_update_cache(self) -> None:
        values = {
            "admin_notify_pulse": False,
            "admin_notify_consent": True,
            "admin_notify_subscriptions": False,
            "admin_notify_open_reminder": True,
            "admin_notify_overdue_reminder": True,
            "admin_notify_monthly_stats": False,
        }
        with patch(
            "aemr_bot.services.notify_toggles.settings_store.get",
            AsyncMock(side_effect=lambda s, key: values[key]),
        ):
            await refresh_cache_from_db(SimpleNamespace())
        for key, expected in values.items():
            assert is_enabled(key) is expected

    @pytest.mark.asyncio
    async def test_none_value_falls_back_to_true(self) -> None:
        """Ключ отсутствует в БД (None) → default True."""
        with patch(
            "aemr_bot.services.notify_toggles.settings_store.get",
            AsyncMock(return_value=None),
        ):
            await refresh_cache_from_db(SimpleNamespace())
        for key in TOGGLE_KEYS:
            assert is_enabled(key) is True

    @pytest.mark.asyncio
    async def test_db_error_keeps_cache_unchanged(self) -> None:
        _cache["admin_notify_pulse"] = False
        with patch(
            "aemr_bot.services.notify_toggles.settings_store.get",
            AsyncMock(side_effect=RuntimeError("db down")),
        ):
            await refresh_cache_from_db(SimpleNamespace())
        # Предыдущее значение сохранилось (best-effort).
        assert is_enabled("admin_notify_pulse") is False
        assert is_enabled("admin_notify_consent") is True

    @pytest.mark.asyncio
    async def test_non_bool_value_coerced(self) -> None:
        """Испорченное значение в БД (не bool) — coerce через bool()."""
        with patch(
            "aemr_bot.services.notify_toggles.settings_store.get",
            AsyncMock(side_effect=lambda s, key: 0 if key == "admin_notify_pulse" else 1),
        ):
            await refresh_cache_from_db(SimpleNamespace())
        assert is_enabled("admin_notify_pulse") is False
        assert is_enabled("admin_notify_consent") is True


class TestResetCacheForTests:
    def test_reset_restores_all_true(self) -> None:
        for key in TOGGLE_KEYS:
            _cache[key] = False
        reset_cache_for_tests()
        for key in TOGGLE_KEYS:
            assert is_enabled(key) is True
