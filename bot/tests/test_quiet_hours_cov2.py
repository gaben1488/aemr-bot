"""Покрытие defensive-нормализации в quiet_hours.refresh_cache_from_db.

Базовый test_quiet_hours.py покрывает enabled True/False и ошибку БД,
но не ветки нормализации, когда settings_store отдаёт None / не-int:
- enabled is None → False (строки 126-127);
- start не int → 18, end не int → 9 (строки 128-131).

Это реальный сценарий: ключи ещё не заданы в settings (свежая БД) или
повреждены. Bot должен подставить безопасные дефолты, а не упасть.

_cache — module-level dict; снимаем снапшот и восстанавливаем, чтобы не
протекало между тестами.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from aemr_bot.services.quiet_hours import _cache, refresh_cache_from_db


@pytest.fixture(autouse=True)
def _restore_cache():
    snapshot = dict(_cache)
    yield
    _cache.clear()
    _cache.update(snapshot)


@pytest.mark.asyncio
async def test_none_enabled_normalized_to_false() -> None:
    with patch(
        "aemr_bot.services.quiet_hours.settings_store.get",
        AsyncMock(side_effect=lambda s, key: {
            "admin_quiet_hours_enabled": None,
            "admin_quiet_hours_start": 20,
            "admin_quiet_hours_end": 8,
        }[key]),
    ):
        await refresh_cache_from_db(SimpleNamespace())
    assert _cache["enabled"] is False
    # start/end валидные — сохраняются как есть.
    assert _cache["start"] == 20
    assert _cache["end"] == 8


@pytest.mark.asyncio
async def test_non_int_start_end_fall_back_to_defaults() -> None:
    with patch(
        "aemr_bot.services.quiet_hours.settings_store.get",
        AsyncMock(side_effect=lambda s, key: {
            "admin_quiet_hours_enabled": True,
            "admin_quiet_hours_start": "not-an-int",
            "admin_quiet_hours_end": None,
        }[key]),
    ):
        await refresh_cache_from_db(SimpleNamespace())
    assert _cache["enabled"] is True
    # Невалидные значения заменены безопасными дефолтами 18 / 9.
    assert _cache["start"] == 18
    assert _cache["end"] == 9
