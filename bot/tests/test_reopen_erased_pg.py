"""A stale reopen button must not reopen an erased appeal."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from aemr_bot.db.models import Appeal, User
from aemr_bot.handlers import admin_appeal_ops
from tests._helpers import make_event


@pytest.mark.asyncio
async def test_reopen_erased_appeal_acknowledges_without_mutation(session, monkeypatch):
    user = User(max_user_id=90421, consent_pdn_at=datetime.now(UTC))
    session.add(user)
    await session.flush()
    appeal = Appeal(user_id=user.id, status="closed", content_erased_at=datetime.now(UTC))
    session.add(appeal)
    await session.commit()
    monkeypatch.setattr(admin_appeal_ops, "ensure_operator", AsyncMock(return_value=True))
    ack = AsyncMock()
    monkeypatch.setattr(admin_appeal_ops, "ack_callback", ack)
    audit = AsyncMock()
    monkeypatch.setattr(admin_appeal_ops.operators_service, "write_audit", audit)
    event = make_event(chat_id=123, user_id=90422, with_callback=True)
    for _ in range(2):
        await admin_appeal_ops.run_reopen(event, appeal.id)
    assert ack.await_count == 2
    assert all("очищен" in call.args[1] for call in ack.await_args_list)
    audit.assert_not_awaited()
    await session.refresh(appeal)
    assert appeal.status == "closed" and appeal.content_erased_at is not None
    event.bot.send_message.assert_not_awaited()
