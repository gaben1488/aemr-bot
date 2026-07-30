"""Single-instance advisory-lock (db/single_instance.py).

- _lock_key: стабильный int64 из токена, разные токены → разные ключи
  (pure, гоняется без БД);
- acquire_single_instance_lock: sqlite → no-op (None); PG → второй
  захват при живом первом бросает SingleInstanceError (в CI на Postgres);
- verify_single_instance_lock (P2-3, watchdog): живое соединение → ok;
  мёртвое + переакквизиция удалась → reacquired; мёртвое + замок занят →
  SingleInstanceError (юниты на моках, без БД).
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock

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


class TestVerifySingleInstanceLock:
    """Watchdog-проверка «замок ещё наш?» — юниты на мок-соединениях."""

    @pytest.mark.asyncio
    async def test_noop_mode_always_ok(self) -> None:
        """sqlite no-op режим (conn=None) — проверять нечего, всегда ok."""
        status, conn = await single_instance.verify_single_instance_lock(None)
        assert status == single_instance.LOCK_OK
        assert conn is None

    @pytest.mark.asyncio
    async def test_live_connection_ok(self) -> None:
        """Соединение живо (SELECT 1 прошёл) → замок держится, conn прежний."""
        live = AsyncMock()
        live.scalar = AsyncMock(return_value=1)
        status, conn = await single_instance.verify_single_instance_lock(live)
        assert status == single_instance.LOCK_OK
        assert conn is live
        live.close.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dead_connection_reacquires(self, monkeypatch) -> None:
        """Соединение мертво, замок свободен → закрыт старый conn,
        взят заново на свежем: статус reacquired, вернулся новый conn."""
        dead = AsyncMock()
        dead.scalar = AsyncMock(side_effect=ConnectionError("db restarted"))
        fresh = AsyncMock()
        monkeypatch.setattr(
            single_instance,
            "acquire_single_instance_lock",
            AsyncMock(return_value=fresh),
        )
        status, conn = await single_instance.verify_single_instance_lock(dead)
        assert status == single_instance.LOCK_REACQUIRED
        assert conn is fresh
        dead.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dead_connection_and_busy_lock_raises(self, monkeypatch) -> None:
        """Соединение мертво И замок уже у другого процесса →
        SingleInstanceError пробрасывается (вызывающий обязан завершить
        процесс — работать вторым экземпляром нельзя)."""
        dead = AsyncMock()
        dead.scalar = AsyncMock(side_effect=ConnectionError("db restarted"))
        monkeypatch.setattr(
            single_instance,
            "acquire_single_instance_lock",
            AsyncMock(side_effect=single_instance.SingleInstanceError("busy")),
        )
        with pytest.raises(single_instance.SingleInstanceError):
            await single_instance.verify_single_instance_lock(dead)


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
            # Снять лок явным pg_advisory_unlock (голый close вернул бы
            # соединение в пул живым — лок остался бы висеть).
            await single_instance.release_single_instance_lock(first)
        # После освобождения захват снова возможен.
        again = await single_instance.acquire_single_instance_lock(eng)
        assert again is not None
        await single_instance.release_single_instance_lock(again)
    finally:
        await eng.dispose()
