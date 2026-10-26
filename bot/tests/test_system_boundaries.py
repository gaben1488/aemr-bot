from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aemr_bot.utils.event import get_ids


def test_callback_fallback_uses_clicker_not_original_message_author():
    event = SimpleNamespace(
        message=SimpleNamespace(recipient=SimpleNamespace(chat_id=500), sender=SimpleNamespace(user_id=1)),
        callback=SimpleNamespace(user=SimpleNamespace(user_id=2)),
    )
    assert get_ids(event) == (500, 2)


def test_callback_without_actor_does_not_impersonate_message_author():
    event = SimpleNamespace(
        message=SimpleNamespace(recipient=SimpleNamespace(chat_id=500), sender=SimpleNamespace(user_id=1)),
        callback=SimpleNamespace(user=None),
    )
    assert get_ids(event) == (500, None)


@pytest.mark.asyncio
async def test_polling_that_never_succeeds_eventually_becomes_unhealthy(monkeypatch):
    from aemr_bot import health, main
    clock = [100.0]
    monkeypatch.setattr(health.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(health.poll_watch, 'last_poll', 0.0)
    bot = SimpleNamespace(get_updates=AsyncMock(side_effect=OSError('offline')))
    main._install_polling_timeout(bot, 30)
    with pytest.raises(OSError):
        await bot.get_updates()
    clock[0] = 1000.0
    with pytest.raises(OSError):
        await bot.get_updates()
    assert not health.poll_watch.is_fresh(max_age=120)


@pytest.mark.asyncio
async def test_quiet_suppression_does_not_log_message_body(monkeypatch, caplog):
    import logging
    from aemr_bot.services import admin_bus, quiet_hours
    monkeypatch.setattr(admin_bus.cfg, 'admin_group_id', 123)
    monkeypatch.setattr(quiet_hours, 'is_quiet_hours_now', lambda: True)
    bot = SimpleNamespace(send_message=AsyncMock())
    secret = 'private resident message'
    with caplog.at_level(logging.INFO):
        assert await admin_bus.send(bot, text=secret) is None
    bot.send_message.assert_not_awaited()
    assert secret not in caplog.text
