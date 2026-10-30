"""Exercise PostgreSQL ordering between draft creation and recovery."""
import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from aemr_bot.db.models import Appeal, Message, Operator, User
from aemr_bot.db.session import session_scope
from aemr_bot.handlers import operator_draft
from aemr_bot.services import appeal_locks, operator_drafts, operator_reply_attempts
from tests._helpers import make_event


@pytest.mark.asyncio
async def test_recovery_and_new_draft_do_not_invert_appeal_locks(session, monkeypatch):
    user = User(max_user_id=90301, consent_pdn_at=datetime.now(UTC))
    operator = Operator(max_user_id=90302, full_name="Operator", role="aemr")
    session.add_all([user, operator])
    await session.flush()
    appeal = Appeal(user_id=user.id, status="new", summary="Test", attachments=[])
    session.add(appeal)
    await session.commit()
    attempt_id = await operator_reply_attempts.reserve(
        appeal_id=appeal.id, operator_id=operator.id, source_key="lock-order-test",
        text="Confirmed reply", attachments=[], is_final=False,
    )
    await operator_reply_attempts.mark_confirmed(attempt_id, "lock-order-mid")
    monkeypatch.setattr(operator_reply_attempts, "_recovery_cursor", 0)
    monkeypatch.setattr(operator_draft, "get_operator", AsyncMock(return_value=operator))
    monkeypatch.setattr(operator_draft, "is_admin_chat", lambda _: True)
    monkeypatch.setattr(operator_draft, "ack_callback", AsyncMock())
    monkeypatch.setattr(operator_draft, "show", AsyncMock())

    barrier_held, draft_waiting, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_hold = appeal_locks.hold

    async def coordinated_hold(db, appeal_id):
        if asyncio.current_task().get_name() == "draft":
            draft_waiting.set()
        await original_hold(db, appeal_id)
        if asyncio.current_task().get_name() == "recovery":
            barrier_held.set()
            await release.wait()

    monkeypatch.setattr(appeal_locks, "hold", coordinated_hold)
    recovery = asyncio.create_task(operator_reply_attempts.recover_confirmed(), name="recovery")
    draft_task = None
    try:
        await asyncio.wait_for(barrier_held.wait(), 5)
        draft_task = asyncio.create_task(
            operator_draft.begin(make_event(chat_id=123, user_id=operator.max_user_id),
                                 appeal.id, is_final=False), name="draft"
        )
        await asyncio.wait_for(draft_waiting.wait(), 5)
        # Recovery must be able to lock the row while draft waits for its barrier.
        async with session_scope() as probe:
            await probe.scalar(select(Appeal.id).where(Appeal.id == appeal.id).with_for_update(nowait=True))
    except BaseException:
        if draft_task is not None:
            draft_task.cancel()
            await asyncio.gather(draft_task, return_exceptions=True)
        raise
    finally:
        release.set()
        await asyncio.wait_for(recovery, 10)
    await asyncio.wait_for(draft_task, 10)
    assert recovery.result() == 1
    async with session_scope() as check:
        draft = await operator_drafts.get(check, operator.id)
        assert draft is not None and draft.appeal_id == appeal.id
        messages = list(await check.scalars(select(Message).where(Message.appeal_id == appeal.id)))
        assert [message.max_message_id for message in messages] == ["lock-order-mid"]
    assert await operator_reply_attempts.recover_confirmed() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_begin_failure_releases_lock_and_keeps_existing_parts(session, monkeypatch, failure):
    user = User(max_user_id=90311, consent_pdn_at=datetime.now(UTC))
    operator = Operator(max_user_id=90312, full_name="Operator", role="aemr")
    session.add_all([user, operator])
    await session.flush()
    appeal = Appeal(user_id=user.id, status="new", summary="Test", attachments=[])
    session.add(appeal)
    await session.flush()
    draft = await operator_drafts.start(session, operator.id, appeal.id, is_final=False)
    token = draft.token
    files = [{"type": "file", "payload": {"token": "synthetic"}}]
    await operator_drafts.append(session, operator.id, token, source="one", text="First", attachments=files)
    await operator_drafts.append(session, operator.id, token, source="two", text="Second", attachments=[])
    await session.commit()
    monkeypatch.setattr(operator_draft, "get_operator", AsyncMock(return_value=operator))
    monkeypatch.setattr(operator_draft, "is_admin_chat", lambda _: True)
    monkeypatch.setattr(operator_draft, "ack_callback", AsyncMock())
    monkeypatch.setattr(operator_draft, "show", AsyncMock())

    async def fail_after_lock(*args, **kwargs):
        if failure == "cancel":
            raise asyncio.CancelledError()
        raise RuntimeError("synthetic error")

    original_get = operator_draft.appeals.get_by_id
    monkeypatch.setattr(operator_draft.appeals, "get_by_id", fail_after_lock)
    error = asyncio.CancelledError if failure == "cancel" else RuntimeError
    with pytest.raises(error):
        await operator_draft.begin(make_event(chat_id=123, user_id=operator.max_user_id),
                                   appeal.id, is_final=True)
    monkeypatch.setattr(operator_draft.appeals, "get_by_id", original_get)
    await operator_draft.begin(make_event(chat_id=123, user_id=operator.max_user_id),
                               appeal.id, is_final=True)
    async with session_scope() as check:
        await asyncio.wait_for(appeal_locks.hold(check, appeal.id), 5)
        restored = await operator_drafts.get(check, operator.id)
        assert restored.token == token and restored.is_final is False
        assert restored.data["summary_chunks"] == ["First", "Second"]
        assert restored.data["attachments"] == files
