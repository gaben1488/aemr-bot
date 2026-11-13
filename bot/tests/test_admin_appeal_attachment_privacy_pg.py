"""Attachment views must remain protected and inventoried through output."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from aemr_bot.db.models import Appeal, ResidentDataCopy
from aemr_bot.handlers import admin_appeal_ops
from aemr_bot.services import admin_relay, data_erasure, users
from tests._erasure_helpers import erasure_database  # noqa: F401
from tests._helpers import make_event


@pytest.fixture
async def attachment_view(erasure_db, monkeypatch):
    monkeypatch.setattr(admin_appeal_ops, "session_scope", erasure_db.scope)
    monkeypatch.setattr(admin_appeal_ops, "ensure_operator", AsyncMock(return_value=True))
    monkeypatch.setattr(admin_appeal_ops, "ack_callback", AsyncMock())
    monkeypatch.setattr(admin_relay, "deserialize_for_relay", lambda _: [])
    async with erasure_db.scope() as session:
        appeal = await session.get(Appeal, erasure_db.appeal_id)
        appeal.attachments = [{"type": "file", "payload": {"token": "synthetic"}}]
    event = make_event(chat_id=123, user_id=17)
    raw_send = AsyncMock(return_value=SimpleNamespace(message_id="warning-mid"))
    event.bot.send_message = raw_send
    data_erasure.install_outgoing_hook(event.bot)
    return erasure_db, event, raw_send


@pytest.mark.asyncio
async def test_available_attachment_is_relayed_with_inventory(attachment_view, monkeypatch):
    db, event, raw_send = attachment_view
    files = [{"type": "file", "payload": {"token": "synthetic"}}]
    monkeypatch.setattr(admin_relay, "deserialize_for_relay", lambda _: files)
    await asyncio.wait_for(admin_appeal_ops.run_show_attachments(event, db.appeal_id), 5)
    assert raw_send.await_count == 1
    assert raw_send.await_args.kwargs["attachments"] == files
    async with db.scope() as session:
        copy = await session.scalar(select(ResidentDataCopy).where(ResidentDataCopy.message_id == "warning-mid"))
        assert copy is not None and copy.appeal_id == db.appeal_id
        assert data_erasure.subject_digest(42) in copy.subjects


@pytest.mark.asyncio
async def test_warning_is_inventoried_and_marked_manual_after_erasure(attachment_view):
    db, event, _ = attachment_view
    await admin_appeal_ops.run_show_attachments(event, db.appeal_id)
    async with db.scope() as session:
        copy = await session.scalar(select(ResidentDataCopy).where(ResidentDataCopy.message_id == "warning-mid"))
        assert copy is not None and copy.appeal_id == db.appeal_id
        assert data_erasure.subject_digest(42) in copy.subjects
        await users.erase_pdn_detailed(session, 42)
    async with db.scope() as session:
        copy = await session.scalar(select(ResidentDataCopy).where(ResidentDataCopy.message_id == "warning-mid"))
        assert copy.erase_requested and copy.state == "manual"


@pytest.mark.asyncio
async def test_old_button_after_erasure_displays_only_generic_notice(attachment_view):
    db, event, raw_send = attachment_view
    async with db.scope() as session:
        await users.erase_pdn_detailed(session, 42)
    await admin_appeal_ops.run_show_attachments(event, db.appeal_id)
    assert raw_send.await_count == 1
    rendered = raw_send.await_args.kwargs["text"]
    assert rendered == "Вложения недоступны."
    assert str(db.appeal_id) not in rendered


@pytest.mark.asyncio
async def test_erasure_waits_until_attachment_warning_is_output(attachment_view, monkeypatch):
    db, event, raw_send = attachment_view
    entered, release = asyncio.Event(), asyncio.Event()
    render = admin_relay.render_appeal_attachments

    async def wait_before_output(*args, **kwargs):
        entered.set()
        await release.wait()
        await render(*args, **kwargs)

    monkeypatch.setattr(admin_relay, "render_appeal_attachments", wait_before_output)
    viewing = asyncio.create_task(admin_appeal_ops.run_show_attachments(event, db.appeal_id))
    erase_task = None
    try:
        await asyncio.wait_for(entered.wait(), 5)

        async def erase():
            async with db.scope() as session:
                return await users.erase_pdn_detailed(session, 42)

        erase_task = asyncio.create_task(erase())
        done, _ = await asyncio.wait({erase_task}, timeout=0.2)
        assert not done, "erase completed while detached attachment view remained active"
    finally:
        release.set()
        if erase_task is not None:
            await asyncio.wait_for(asyncio.gather(viewing, erase_task), 10)
        else:
            await asyncio.wait_for(viewing, 10)
    assert raw_send.await_count == 1
    async with db.scope() as session:
        copy = await session.scalar(select(ResidentDataCopy).where(ResidentDataCopy.message_id == "warning-mid"))
        assert copy is not None and copy.erase_requested and copy.state == "manual"
