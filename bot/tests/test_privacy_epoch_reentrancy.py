"""Nested readers must not queue behind an eraser waiting for their own outer lock."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from aemr_bot.db.session import SessionFactory
from aemr_bot.services import data_erasure, privacy_epoch


async def _wait_for_exclusive_waiter(pid):
    async with asyncio.timeout(2):
        async with SessionFactory() as observer:
            while not await observer.scalar(text(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=:pid "
                "AND locktype='advisory' AND NOT granted)"), {"pid": pid}):
                await asyncio.sleep(.005)


@pytest.mark.asyncio
@pytest.mark.parametrize("via_hook", [False, True])
async def test_nested_tracked_send_completes_with_queued_exclusive_eraser(session, via_hook):
    writer_started = asyncio.Future()
    async def erase():
        async with SessionFactory.begin() as writer:
            writer_started.set_result(await writer.scalar(text("SELECT pg_backend_pid()")))
            await privacy_epoch.mark_erasure(writer)
    async with SessionFactory.begin() as outer:
        await privacy_epoch.hold_delivery(outer)
        erasing = asyncio.create_task(erase())
        try:
            await _wait_for_exclusive_waiter(await writer_started)
            send = AsyncMock(return_value=SimpleNamespace(message_id="nested-copy"))
            bot = SimpleNamespace(send_message=send)
            # asyncio.timeout preserves this task; wait_for would create a child.
            async with asyncio.timeout(2):
                if via_hook:
                    data_erasure.install_outgoing_hook(bot)
                    await bot.send_message(user_id=42, text="screen")
                else:
                    await data_erasure.tracked_send(bot, kind="bulk_export", text="export")
            send.assert_awaited_once()
            assert not erasing.done()
        finally:
            if not erasing.done():
                erasing.cancel()
            await asyncio.gather(erasing, return_exceptions=True)


@pytest.mark.asyncio
async def test_child_delivery_keeps_its_lock_after_parent_transaction_ends(session):
    acquired, release = asyncio.Event(), asyncio.Event()
    async def child_delivery():
        async with SessionFactory.begin() as child:
            await privacy_epoch.hold_delivery(child)
            acquired.set()
            await release.wait()
    async with SessionFactory.begin() as outer:
        await privacy_epoch.hold_delivery(outer)
        child = asyncio.create_task(child_delivery())
        await acquired.wait()
    writer_started = asyncio.Future()
    async def erase():
        async with SessionFactory.begin() as writer:
            writer_started.set_result(await writer.scalar(text("SELECT pg_backend_pid()")))
            await privacy_epoch.mark_erasure(writer)
    erasing = asyncio.create_task(erase())
    try:
        await _wait_for_exclusive_waiter(await writer_started)
        assert not erasing.done()
        release.set()
        async with asyncio.timeout(2):
            await child
            await erasing
    finally:
        release.set()
        for task in (child, erasing):
            if not task.done():
                task.cancel()
        await asyncio.gather(child, erasing, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["commit", "rollback", "close"])
async def test_completed_outer_transaction_cannot_supply_a_later_delivery_lock(session, end):
    async with SessionFactory() as outer:
        await privacy_epoch.hold_delivery(outer)
        await getattr(outer, end)()
        async with SessionFactory.begin() as later:
            await privacy_epoch.hold_delivery(later)
            # A stale context entry would return without locking this backend.
            assert await later.scalar(text(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=pg_backend_pid() "
                "AND locktype='advisory' AND mode='ShareLock' AND granted)"))
