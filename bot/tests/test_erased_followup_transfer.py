from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from aemr_bot.db.models import Appeal, DialogState, User
from tests._helpers import make_event


@pytest.mark.asyncio
async def test_cleared_followup_becomes_new_draft_without_retyping_parts(session, monkeypatch):
    from aemr_bot.handlers import appeal as handler

    files = [{"type": "file", "payload": {"token": "saved-file"}}]
    user = User(max_user_id=771901, first_name="Анна", consent_pdn_at=datetime.now(UTC),
                dialog_state=DialogState.AWAITING_FOLLOWUP_TEXT.value)
    session.add(user)
    await session.flush()
    source = Appeal(user_id=user.id, summary=None, address=None, locality=None,
                    status="new", content_erased_at=datetime.now(UTC))
    session.add(source)
    await session.flush()
    user.dialog_data = {"appeal_id": source.id, "draft_token": "saved-token",
                        "summary_chunks": ["Первая часть", "Вторая часть"], "attachments": files}
    await session.flush()

    @asynccontextmanager
    async def scope():
        yield session
    ask = AsyncMock()
    monkeypatch.setattr(handler, "session_scope", scope)
    monkeypatch.setattr(handler.appeal_funnel, "ask_locality", ask)
    event = make_event(user_id=user.max_user_id)
    await handler._cb_draft(event, user.max_user_id, "draft:new_related:saved-token")
    assert user.dialog_state == DialogState.AWAITING_LOCALITY.value
    assert user.dialog_data["summary_chunks"] == ["Первая часть", "Вторая часть"]
    assert user.dialog_data["attachments"] == files
    assert user.dialog_data["repeat_source_appeal_id"] == source.id
    assert "appeal_id" not in user.dialog_data
    assert user.dialog_data["draft_token"] != "saved-token"
    assert await session.scalar(select(func.count()).select_from(Appeal)) == 1
    ask.assert_awaited_once()
    snapshot = dict(user.dialog_data)
    await handler._cb_draft(event, user.max_user_id, "draft:new_related:saved-token")
    assert user.dialog_data == snapshot
