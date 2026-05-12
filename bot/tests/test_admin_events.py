"""Тесты служебных уведомлений о действиях жителя."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_notify_consent_given_sends_to_admin_group() -> None:
    from aemr_bot.services import admin_events

    bot = AsyncMock()
    with patch("aemr_bot.services.admin_events.cfg.admin_group_id", 777):
        await admin_events.notify_consent_given(bot, max_user_id=42)

    bot.send_message.assert_called_once()
    assert bot.send_message.call_args.kwargs["chat_id"] == 777
    text = bot.send_message.call_args.kwargs["text"]
    assert "согласие" in text.lower()
    assert "42" in text


@pytest.mark.asyncio
async def test_notify_skips_when_admin_group_is_not_configured() -> None:
    from aemr_bot.services import admin_events

    bot = AsyncMock()
    with patch("aemr_bot.services.admin_events.cfg.admin_group_id", None):
        await admin_events.notify_broadcast_subscribed(bot, max_user_id=42)

    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_notify_logs_and_does_not_raise_on_delivery_error() -> None:
    from aemr_bot.services import admin_events

    bot = AsyncMock()
    bot.send_message.side_effect = RuntimeError("MAX unavailable")

    with patch("aemr_bot.services.admin_events.cfg.admin_group_id", 777):
        await admin_events.notify_broadcast_unsubscribed(
            bot,
            max_user_id=42,
            source="меню",
        )

    bot.send_message.assert_called_once()
