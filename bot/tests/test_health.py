"""Тесты на bot/aemr_bot/health.py — Heartbeat и /healthz endpoint."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from aemr_bot.health import Heartbeat


class TestHeartbeat:
    def test_initial_not_fresh(self) -> None:
        hb = Heartbeat()
        assert not hb.is_fresh()

    def test_after_beat_fresh(self) -> None:
        hb = Heartbeat()
        hb.beat()
        assert hb.is_fresh()

    def test_after_beat_with_explicit_max_age(self) -> None:
        hb = Heartbeat()
        hb.beat()
        # max_age=10 секунд — только что обновлённый точно считается fresh.
        # max_age=0 не подходит: между beat() и вызовом is_fresh() идут
        # микросекунды, и 0 <= 0.000001 == False.
        assert hb.is_fresh(max_age=10.0)

    def test_stale_after_long_time(self) -> None:
        hb = Heartbeat()
        hb.beat()
        # Подменяем last_beat в прошлое
        hb.last_beat = time.monotonic() - 10000.0
        assert not hb.is_fresh(max_age=60.0)


class TestPingDb:
    @pytest.mark.asyncio
    async def test_ping_returns_true_on_success(self) -> None:
        from aemr_bot import health

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock()

        # Mock context manager
        class _Scope:
            async def __aenter__(self):
                return mock_session

            async def __aexit__(self, *args):
                return None

        with patch("aemr_bot.db.session.session_scope", return_value=_Scope()):
            result = await health._ping_db()
        assert result is True

    @pytest.mark.asyncio
    async def test_ping_returns_false_on_db_error(self) -> None:
        from aemr_bot import health

        with patch(
            "aemr_bot.db.session.session_scope",
            side_effect=RuntimeError("db down"),
        ):
            result = await health._ping_db()
        assert result is False
