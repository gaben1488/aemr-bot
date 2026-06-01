"""Покрытие services/wizard_persist без реального Postgres.

`_upsert` использует pg_insert.on_conflict_do_update (dialect-specific,
тестируется на PG в CI). Но `hydrate_into_registry` и `_ttl_for` —
backend-независимая логика: первая раскладывает строки БД по in-memory
registry, и её можно проверить с замоканной AsyncSession.

Покрываем:
- _ttl_for: обе ветки (op vs broadcast/прочее).
- hydrate_into_registry: смешанные строки (op/broadcast/unknown-kind →
  warning+skip), счётчики, GC-лог (rowcount>0) и его отсутствие (=0),
  пустой результат.
"""
from __future__ import annotations

from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest

from aemr_bot.services import wizard_persist as wp
from aemr_bot.services import wizard_registry as wr


@pytest.fixture(autouse=True)
def _clean_registry():
    wr.reset_all()
    yield
    wr.reset_all()


def _mock_session(rows: list, *, gc_rowcount: int = 0) -> MagicMock:
    """AsyncSession-заглушка: execute() (delete-GC) → rowcount,
    scalars() → объект с .all()==rows."""
    session = MagicMock()
    session.execute = AsyncMock(return_value=NS(rowcount=gc_rowcount))
    scalars_result = MagicMock()
    scalars_result.all = MagicMock(return_value=rows)
    session.scalars = AsyncMock(return_value=scalars_result)
    return session


class TestTtlFor:
    def test_op_ttl(self) -> None:
        assert wp._ttl_for(wp.KIND_OP) == wp.TTL_OP_SEC

    def test_broadcast_ttl(self) -> None:
        assert wp._ttl_for(wp.KIND_BROADCAST) == wp.TTL_BROADCAST_SEC

    def test_unknown_kind_defaults_to_broadcast_ttl(self) -> None:
        # _ttl_for: всё, что не op → broadcast TTL.
        assert wp._ttl_for("anything-else") == wp.TTL_BROADCAST_SEC


class TestHydrateIntoRegistry:
    @pytest.mark.asyncio
    async def test_mixed_rows_dispatched_and_counted(self) -> None:
        rows = [
            NS(kind="op", operator_max_user_id=10, state={"step": "x"}, id=1),
            NS(kind="broadcast", operator_max_user_id=20, state={"t": "hi"}, id=2),
            # неизвестный kind → warning + skip, не считается.
            NS(kind="weird", operator_max_user_id=30, state=None, id=3),
        ]
        session = _mock_session(rows, gc_rowcount=2)

        op_count, bcast_count = await wp.hydrate_into_registry(session)

        assert op_count == 1
        assert bcast_count == 1
        assert wr.get_op_wizard(10) == {"step": "x"}
        assert wr.get_broadcast_wizard(20) == {"t": "hi"}
        # неизвестный kind в registry не попал.
        assert wr.get_op_wizard(30) is None

    @pytest.mark.asyncio
    async def test_no_expired_rows_skips_gc_log(self) -> None:
        # gc_rowcount=0 → ветка `if deleted.rowcount` ложна (лог не пишем).
        rows = [NS(kind="op", operator_max_user_id=11, state={"a": 1}, id=5)]
        session = _mock_session(rows, gc_rowcount=0)

        op_count, bcast_count = await wp.hydrate_into_registry(session)
        assert (op_count, bcast_count) == (1, 0)
        assert wr.get_op_wizard(11) == {"a": 1}

    @pytest.mark.asyncio
    async def test_empty_result_returns_zero(self) -> None:
        session = _mock_session([], gc_rowcount=0)
        assert await wp.hydrate_into_registry(session) == (0, 0)

    @pytest.mark.asyncio
    async def test_none_state_normalized_to_empty_dict(self) -> None:
        # row.state=None → в registry кладём {} (dict(row.state or {})).
        rows = [NS(kind="op", operator_max_user_id=12, state=None, id=7)]
        session = _mock_session(rows, gc_rowcount=1)

        await wp.hydrate_into_registry(session)
        assert wr.get_op_wizard(12) == {}
