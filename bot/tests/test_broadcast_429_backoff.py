"""Тесты на 429-handling в `_send_one` (MAXAPI_DEEP_DIVE §14 P1)
и на `_extract_retry_after` helper.

Полный broadcast loop (`_run_broadcast_impl`) уже покрыт в
test_broadcast_handlers.py; здесь — только узкий контракт нового
поведения вокруг rate-limit'а.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


pytest.importorskip("maxapi")


class TestExtractRetryAfter:
    def test_returns_none_when_no_raw(self) -> None:
        from aemr_bot.handlers.broadcast import _extract_retry_after
        exc = RuntimeError("plain")
        assert _extract_retry_after(exc) is None

    def test_extracts_from_raw_dict(self) -> None:
        from aemr_bot.handlers.broadcast import _extract_retry_after
        exc = SimpleNamespace(raw={"retry_after": "5.5"})
        assert _extract_retry_after(exc) == 5.5

    def test_handles_int_value(self) -> None:
        from aemr_bot.handlers.broadcast import _extract_retry_after
        exc = SimpleNamespace(raw={"Retry-After": 10})
        assert _extract_retry_after(exc) == 10.0

    def test_camelcase_variant(self) -> None:
        from aemr_bot.handlers.broadcast import _extract_retry_after
        exc = SimpleNamespace(raw={"retryAfter": 3})
        assert _extract_retry_after(exc) == 3.0

    def test_invalid_value_returns_none(self) -> None:
        from aemr_bot.handlers.broadcast import _extract_retry_after
        exc = SimpleNamespace(raw={"retry_after": "не-число"})
        assert _extract_retry_after(exc) is None

    def test_raw_not_dict_returns_none(self) -> None:
        from aemr_bot.handlers.broadcast import _extract_retry_after
        exc = SimpleNamespace(raw="some string body")
        assert _extract_retry_after(exc) is None


class TestSendOne429Backoff:
    """Полная асинхронная цепочка с моком bot — проверяем что 429
    приводит к retry, а обычная ошибка — к immediate return.
    """

    @pytest.mark.asyncio
    async def test_429_retries_then_succeeds(self, monkeypatch) -> None:
        from aemr_bot.handlers import broadcast

        # Считаем вызовы и поведение: первые 2 раза 429, 3-й успех.
        calls = {"count": 0}

        async def fake_send_message(**kwargs):
            calls["count"] += 1
            if calls["count"] <= 2:
                raise RuntimeError("HTTP 429 Too Many Requests")
            return SimpleNamespace(body=SimpleNamespace(mid="ok"))

        # Patch asyncio.sleep чтобы тест не реально ждал секунды
        async def fake_sleep(s):
            pass

        monkeypatch.setattr(broadcast.asyncio, "sleep", fake_sleep)

        bot = SimpleNamespace(send_message=fake_send_message)
        result = await broadcast._send_one(
            bot, max_user_id=42, body_text="hi"
        )
        assert result is None  # успех
        assert calls["count"] == 3  # три попытки

    @pytest.mark.asyncio
    async def test_429_three_times_returns_error(self, monkeypatch) -> None:
        from aemr_bot.handlers import broadcast

        async def fake_send_message(**kwargs):
            raise RuntimeError("HTTP 429 rate limit")

        async def fake_sleep(s):
            pass

        monkeypatch.setattr(broadcast.asyncio, "sleep", fake_sleep)

        bot = SimpleNamespace(send_message=fake_send_message)
        result = await broadcast._send_one(
            bot, max_user_id=42, body_text="hi"
        )
        # После 3 попыток — error string возвращён, не None
        assert result is not None
        assert "429" in result

    @pytest.mark.asyncio
    async def test_non_rate_limit_error_no_retry(self) -> None:
        """Не-429 ошибка возвращается сразу, без retry."""
        from aemr_bot.handlers import broadcast

        calls = {"count": 0}

        async def fake_send_message(**kwargs):
            calls["count"] += 1
            raise RuntimeError("user not found")

        bot = SimpleNamespace(send_message=fake_send_message)
        result = await broadcast._send_one(
            bot, max_user_id=42, body_text="hi"
        )
        assert result is not None
        assert "user not found" in result
        # Только одна попытка — не ретраим обычные ошибки
        assert calls["count"] == 1
