"""Exercise durable receipt SQL and commit recovery on PostgreSQL in CI."""
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select

from aemr_bot.db.models import InboxEvent
from aemr_bot.services import inbox


@pytest.mark.asyncio
async def test_pg_receipt_dedup_recovery_and_erasure(session, monkeypatch):
    @asynccontextmanager
    async def scope():
        yield session
        await session.commit()
    monkeypatch.setattr(inbox, 'session_scope', scope)
    raw = {'update_type': 'bot_started', 'timestamp': 1,
           'user': {'user_id': 91002, 'first_name': 'Synthetic'}}
    await inbox.accept([raw, raw])
    rows = (await session.scalars(select(InboxEvent))).all()
    assert len(rows) == 1
    assert await inbox.take_next()
    await inbox.recover_interrupted()
    await session.refresh(rows[0])
    assert rows[0].state == 'failed'
    assert rows[0].payload == {}
    assert await inbox.take_next() is None
    await inbox.erase_pending(session, 91002)
    await session.flush()
    await session.refresh(rows[0])
    assert rows[0].max_user_id is None
