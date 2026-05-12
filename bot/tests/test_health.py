"""Тесты на bot/aemr_bot/health.py — Heartbeat и health endpoints."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
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


class TestHealthEndpoints:
    @pytest.mark.asyncio
    async def test_livez_checks_heartbeat_only_not_db(self) -> None:
        from aemr_bot import health

        request = SimpleNamespace(remote="127.0.0.1")
        with (
            patch.object(health.heartbeat, "is_fresh", return_value=True),
            patch(
                "aemr_bot.health._ping_db_cached",
                new=AsyncMock(side_effect=AssertionError("DB must not be pinged")),
            ),
        ):
            response = await health._livez(request)

        assert response.status == 200
        payload = json.loads(response.text)
        assert payload["ok"] is True
        assert payload["heartbeat_fresh"] is True
        assert "db_ok" not in payload

    @pytest.mark.asyncio
    async def test_readyz_requires_db(self) -> None:
        from aemr_bot import health

        request = SimpleNamespace(remote="127.0.0.1")
        with (
            patch.object(health.heartbeat, "is_fresh", return_value=True),
            patch("aemr_bot.health._ping_db_cached", new=AsyncMock(return_value=False)),
        ):
            response = await health._readyz(request)

        assert response.status == 503
        payload = json.loads(response.text)
        assert payload["ok"] is False
        assert payload["heartbeat_fresh"] is True
        assert payload["db_ok"] is False

    @pytest.mark.asyncio
    async def test_healthz_keeps_readiness_semantics(self) -> None:
        from aemr_bot import health

        request = SimpleNamespace(remote="127.0.0.1")
        with (
            patch.object(health.heartbeat, "is_fresh", return_value=True),
            patch("aemr_bot.health._ping_db_cached", new=AsyncMock(return_value=True)),
        ):
            response = await health._healthz(request)

        assert response.status == 200
        payload = json.loads(response.text)
        assert payload["ok"] is True
        assert payload["db_ok"] is True
