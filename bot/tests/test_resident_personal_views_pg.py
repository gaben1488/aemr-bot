"""Resident read screens stay ordered with retention and retain copy ownership."""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from aemr_bot.db.models import Appeal, Message, ResidentDataCopy, User
from aemr_bot.handlers import menu
from aemr_bot.services import appeals, data_erasure, privacy_epoch
from aemr_bot.utils.resident_lifecycle import guard
from tests._helpers import make_event


async def _old_appeal(session):
    old = datetime.now(UTC) - timedelta(days=365 * 7)
    user = User(max_user_id=42, first_name="Анна", consent_pdn_at=old)
    session.add(user)
    await session.flush()
    appeal = Appeal(user_id=user.id, status="closed", summary="Сохранённый частный вопрос",
                    address="Частный адрес", topic="Дороги", locality="Елизово",
                    created_at=old, closed_at=old, attachments=[{"type": "file", "payload": {"token": "owned-token"}}])
    session.add(appeal)
    await session.flush()
    session.add(Message(appeal_id=appeal.id, direction="from_operator", text="Частный ответ",
                        created_at=old, attachments=[]))
    await session.commit()
    return appeal.id


@pytest.mark.asyncio
@pytest.mark.parametrize("screen", ["card", "history", "list", "attachments", "record_attachments"])
async def test_retention_waits_from_resident_view_read_through_tracked_send(session, monkeypatch, screen):
    appeal_id = await _old_appeal(session)
    loaded, release, purge_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    method = "list_for_user" if screen == "list" else "get_by_id_with_messages"
    original_query = getattr(menu.appeals_service, method)
    original_erasure = privacy_epoch.mark_erasure

    async def delayed_query(*args, **kwargs):
        result = await original_query(*args, **kwargs)
        loaded.set()
        await release.wait()
        return result

    async def erasure_started(db):
        purge_started.set()
        await original_erasure(db)

    monkeypatch.setattr(menu.appeals_service, method, delayed_query)
    monkeypatch.setattr(privacy_epoch, "mark_erasure", erasure_started)
    event = make_event(chat_id=42, user_id=42)
    event.bot.send_message = AsyncMock(return_value=SimpleNamespace(body=SimpleNamespace(mid="resident-view")))
    data_erasure.install_outgoing_hook(event.bot)

    async def view():
        async with guard(42):
            if screen == "card":
                await menu.show_appeal(event, appeal_id, 42)
            elif screen == "history":
                await menu.show_appeal_history(event, appeal_id, 1, 42)
            elif screen == "attachments":
                await menu.show_appeal_attachments(event, appeal_id, 42)
            elif screen == "record_attachments":
                await menu.show_record_attachments(event, appeal_id, 0, 42)
            else:
                await menu.open_my_appeals(event, 42)

    async def retain():
        async with menu.session_scope() as db:
            return await appeals.purge_old_appeals_content(db)

    reading = asyncio.create_task(view())
    purging = None
    try:
        await asyncio.wait_for(loaded.wait(), 3)
        purging = asyncio.create_task(retain())
        await asyncio.wait_for(purge_started.wait(), 3)
        # The real exclusive retention lock cannot pass a live read screen.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(purging), .1)
    finally:
        release.set()
        results = await asyncio.gather(reading, *([purging] if purging else []), return_exceptions=True)
    assert not any(isinstance(result, BaseException) for result in results), results
    copy = await session.scalar(select(ResidentDataCopy).where(ResidentDataCopy.message_id == "resident-view"))
    assert copy is not None and copy.erase_requested and copy.state == "pending"
    if screen != "list":
        assert copy.appeal_id == appeal_id
    assert copy.subjects == [data_erasure.subject_digest(42)]
    await session.refresh(await session.get(Appeal, appeal_id))
    assert (await session.get(Appeal, appeal_id)).summary is None
