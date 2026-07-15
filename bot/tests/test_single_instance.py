"""Single-instance advisory-lock (db/single_instance.py).

- _lock_key: стабильный int64 из токена, разные токены → разные ключи
  (pure, гоняется без БД);
- acquire_single_instance_lock: sqlite → no-op (None); PG → второй
  захват при живом первом бросает SingleInstanceError (в CI на Postgres).
"""
from __future__ import annotations

import os

import pytest

from aemr_bot.db import single_instance

DATABASE_URL = os.environ.get("DATABASE_URL", "")
_HAS_PG = DATABASE_URL.startswith("postgresql")


def test_lock_key_is_stable_and_int64() -> None:
    k1 = single_instance._lock_key("token-abc")
    k2 = single_instance._lock_key("token-abc")
    assert k1 == k2  # детерминирован (не hash() с PYTHONHASHSEED)
    assert -(2**63) <= k1 < 2**63  # влезает в bigint pg_advisory_lock


def test_lock_key_differs_per_token() -> None:
    assert single_instance._lock_key("bot-A") != single_instance._lock_key("bot-B")


@pytest.mark.asyncio
async def test_sqlite_is_noop() -> None:
    """SQLite advisory-lock не поддерживает — acquire отдаёт None, не падает."""
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        conn = await single_instance.acquire_single_instance_lock(eng)
        assert conn is None
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_pg_second_acquire_refused() -> None:
    """На Postgres: пока первый процесс держит лок, второй захват того же
    ключа отклоняется SingleInstanceError (защита от двух экземпляров)."""
    if not _HAS_PG:
        pytest.skip("requires PostgreSQL")
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(DATABASE_URL)
    try:
        first = await single_instance.acquire_single_instance_lock(eng)
        assert first is not None
        try:
            with pytest.raises(single_instance.SingleInstanceError):
                await single_instance.acquire_single_instance_lock(eng)
        finally:
            # Снять лок — освобождается закрытием держащего соединения.
            await first.close()
        # После освобождения захват снова возможен.
        again = await single_instance.acquire_single_instance_lock(eng)
        assert again is not None
        await again.close()
    finally:
        await eng.dispose()
