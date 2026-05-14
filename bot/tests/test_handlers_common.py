"""Тесты `handlers/_common.py` — общих хелперов хендлеров.

`current_user` — контекст-менеджер, свернувший ~30 повторов
`session_scope() + users_service.get_or_create(...)`. Тесты фиксируют
его контракт: отдаёт пару `(session, user)`, прокидывает `max_user_id`
и `first_name` в `get_or_create`, наследует транзакционные границы
`session_scope` (commit на выходе, rollback на исключении).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("maxapi", reason="_common тянет handlers-цепочку")

from aemr_bot.handlers import _common  # noqa: E402


@asynccontextmanager
async def _fake_scope(session):
    """session_scope-подобный CM, отдающий заранее заданную сессию."""
    yield session


class TestCurrentUser:
    @pytest.mark.asyncio
    async def test_yields_session_and_user_pair(self) -> None:
        session = MagicMock(name="session")
        user = MagicMock(name="user")
        get_or_create = AsyncMock(return_value=user)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(_common, "session_scope", lambda: _fake_scope(session))
            mp.setattr(_common.users_service, "get_or_create", get_or_create)
            async with _common.current_user(42) as (got_session, got_user):
                assert got_session is session
                assert got_user is user

    @pytest.mark.asyncio
    async def test_passes_max_user_id_and_first_name(self) -> None:
        session = MagicMock()
        get_or_create = AsyncMock(return_value=MagicMock())
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(_common, "session_scope", lambda: _fake_scope(session))
            mp.setattr(_common.users_service, "get_or_create", get_or_create)
            async with _common.current_user(7, first_name="Аня"):
                pass
        get_or_create.assert_awaited_once_with(
            session, max_user_id=7, first_name="Аня"
        )

    @pytest.mark.asyncio
    async def test_first_name_defaults_to_none(self) -> None:
        session = MagicMock()
        get_or_create = AsyncMock(return_value=MagicMock())
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(_common, "session_scope", lambda: _fake_scope(session))
            mp.setattr(_common.users_service, "get_or_create", get_or_create)
            async with _common.current_user(7):
                pass
        get_or_create.assert_awaited_once_with(
            session, max_user_id=7, first_name=None
        )

    @pytest.mark.asyncio
    async def test_exception_inside_block_propagates(self) -> None:
        # Тело `async with` бросило — исключение должно пройти насквозь,
        # как и у голого session_scope (там оно триггерит rollback).
        session = MagicMock()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(_common, "session_scope", lambda: _fake_scope(session))
            mp.setattr(
                _common.users_service,
                "get_or_create",
                AsyncMock(return_value=MagicMock()),
            )
            with pytest.raises(RuntimeError, match="boom"):
                async with _common.current_user(7):
                    raise RuntimeError("boom")
