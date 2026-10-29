"""Durable follow-up notifications, against PostgreSQL."""
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from aemr_bot.db.models import Appeal, Message, User
from aemr_bot.services import admin_card, followup_delivery
from aemr_bot.services import admin_relay


@pytest.mark.asyncio
async def test_followup_claim_sends_once_and_records_partial_delivery(session, monkeypatch):
    user = User(max_user_id=72010)
    session.add(user)
    await session.flush()
    appeal = Appeal(user_id=user.id, status="in_progress", summary="Исходное", attachments=[])
    session.add(appeal)
    await session.flush()
    message = Message(appeal_id=appeal.id, direction="from_user", text="Дополнение",
                      attachments=[{"type": "file", "payload": {}}], admin_notice_state="pending")
    session.add(message)
    await session.commit()
    render = AsyncMock(return_value="card-mid")
    relay = AsyncMock(return_value=False)
    monkeypatch.setattr(admin_card, "render", render)
    monkeypatch.setattr(admin_relay, "relay_attachments_to_admin", relay)

    assert await followup_delivery.deliver(object(), message.id)
    assert not await followup_delivery.deliver(object(), message.id)
    assert render.await_count == 1
    assert relay.await_count == 1
    assert await session.scalar(select(Message.admin_notice_state).where(Message.id == message.id)) == "partial"


@pytest.mark.asyncio
async def test_unknown_followup_result_is_not_automatically_replayed(session, monkeypatch):
    user = User(max_user_id=72011)
    session.add(user)
    await session.flush()
    appeal = Appeal(user_id=user.id, status="new", summary="Исходное", attachments=[])
    session.add(appeal)
    await session.flush()
    message = Message(appeal_id=appeal.id, direction="from_user", text="Дополнение",
                      attachments=[], admin_notice_state="pending")
    session.add(message)
    await session.commit()
    render = AsyncMock(side_effect=TimeoutError())
    monkeypatch.setattr(admin_card, "render", render)

    assert not await followup_delivery.deliver(object(), message.id)
    assert await session.scalar(select(Message.admin_notice_state).where(Message.id == message.id)) == "unknown"
    assert await followup_delivery.recover_pending(object()) == 0
    assert render.await_count == 1
