import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aemr_bot.handlers import broadcast
from aemr_bot.services import admin_bus


@pytest.fixture(autouse=True)
def isolate_event_loop_gates(monkeypatch):
    from weakref import WeakValueDictionary
    from aemr_bot.utils import resident_lifecycle
    monkeypatch.setattr(resident_lifecycle, "_gates", WeakValueDictionary())


@pytest.mark.asyncio
async def test_stop_while_waiting_global_limiter_prevents_http_send(monkeypatch):
    waiting, release = asyncio.Event(), asyncio.Event()
    cancelled = False
    async def slot():
        waiting.set()
        await release.wait()
    async def allowed():
        return not cancelled
    send = AsyncMock(return_value=SimpleNamespace(message=SimpleNamespace(body=SimpleNamespace(mid='synthetic-mid'))))
    bot = SimpleNamespace(send_message=send)
    monkeypatch.setattr(admin_bus, '_acquire_outgoing_slot', slot)
    admin_bus.install_outgoing_tracker_hook(bot)
    task = asyncio.create_task(broadcast._send_one(bot, 91002, 'synthetic', can_send=allowed))
    await waiting.wait()
    cancelled = True
    release.set()
    with pytest.raises(broadcast._BroadcastCancelled):
        await task
    send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('rollback', [False, True])
async def test_resident_gate_is_held_through_transaction_end(rollback):
    from sqlalchemy.ext.asyncio import AsyncSession

    from aemr_bot.utils.resident_lifecycle import guard, hold_until_commit
    session = AsyncSession()
    waiting, entered = asyncio.Event(), asyncio.Event()
    async def next_event():
        waiting.set()
        async with guard(91002):
            entered.set()
    # /forget may acquire the same gate again inside its own event handler.
    async with guard(91002):
        await hold_until_commit(session, 91002)
    task = asyncio.create_task(next_event())
    try:
        await waiting.wait()
        assert not entered.is_set()
        if rollback:
            await session.rollback()
        else:
            await session.commit()
        await asyncio.wait_for(entered.wait(), 1)
        await task
    finally:
        await session.close()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
