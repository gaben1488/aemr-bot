from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from aemr_bot.db.models import Appeal, Message, Operator, OperatorReplyAttempt
from aemr_bot.services import appeals, data_erasure, users
from aemr_bot.services import operator_reply_attempts as attempts
from tests._erasure_helpers import erasure_database  # noqa: F401


@pytest.mark.asyncio
async def test_erasure_removes_partial_plan_and_never_deletes_external(erasure_db, monkeypatch):
    db = erasure_db
    monkeypatch.setattr(attempts, "session_scope", db.scope)
    async with db.scope() as s:
        op = Operator(max_user_id=90011, full_name="synthetic", role="it")
        s.add(op)
        await s.flush()
        operator_id = op.id
    files = [
        {"type": "file", "payload": {"token": "synthetic-one"}},
        {"type": "file", "payload": {"token": "synthetic-two"}},
    ]
    attempt_id = await attempts.reserve(
        appeal_id=db.appeal_id,
        operator_id=operator_id,
        source_key="synthetic-partial",
        text="private",
        attachments=files,
        is_final=True,
        part_plan=[[0], [1]],
    )
    await attempts.note_part(attempt_id, 0, "sending", None)
    await attempts.note_part(attempt_id, 0, "confirmed", "synthetic-mid")
    await attempts.note_part(attempt_id, 1, "sending", None)
    await attempts.mark_unknown(attempt_id)
    async with db.scope() as s:
        await users.erase_pdn_detailed(s, 42)
    fake = SimpleNamespace(delete_message=AsyncMock())
    await data_erasure.recover_deletions(fake)
    fake.delete_message.assert_not_awaited()
    async with db.scope() as s:
        assert await s.get(OperatorReplyAttempt, attempt_id) is None
        assert await s.scalar(select(Message.id).where(Message.text == "private")) is None
        await data_erasure.replay_restore_tombstones(s)
        assert await s.get(OperatorReplyAttempt, attempt_id) is None


@pytest.mark.asyncio
async def test_late_uncertain_delivery_blocks_retention_until_upper_bound(erasure_db, monkeypatch):
    db = erasure_db
    monkeypatch.setattr(attempts, "session_scope", db.scope)
    old = datetime.now(UTC) - timedelta(days=365 * 7)
    async with db.scope() as s:
        op = Operator(max_user_id=90012, full_name="synthetic", role="it")
        s.add(op)
        appeal = await s.get(Appeal, db.appeal_id)
        appeal.created_at = old
        appeal.status = "new"
        message = await s.scalar(select(Message).where(Message.appeal_id == appeal.id))
        message.created_at = old
        await s.flush()
        opid = op.id
    attempt_id = await attempts.reserve(
        appeal_id=db.appeal_id,
        operator_id=opid,
        source_key="synthetic-retention",
        text="reply",
        attachments=[],
        is_final=True,
        part_plan=[[]],
    )
    await attempts.note_part(attempt_id, 0, "sending", None)
    await attempts.mark_unknown(attempt_id)
    async with db.scope() as s:
        attempt = await s.get(OperatorReplyAttempt, attempt_id)
        attempt.created_at = old
    async with db.scope() as s:
        assert await attempts.resolve_unknown(s, attempt_id, delivered=True, actor_max_id=90012)
    async with db.scope() as s:
        await appeals.purge_old_appeals_content(s)
        appeal = await s.get(Appeal, db.appeal_id)
        assert appeal.summary == "Private" and appeal.content_erased_at is None


@pytest.mark.asyncio
async def test_recent_unknown_attempt_prevents_old_appeal_retention(erasure_db, monkeypatch):
    db = erasure_db
    monkeypatch.setattr(attempts, "session_scope", db.scope)
    old = datetime.now(UTC) - timedelta(days=365 * 7)
    async with db.scope() as s:
        op = Operator(max_user_id=90013, full_name="synthetic", role="it")
        s.add(op)
        appeal = await s.get(Appeal, db.appeal_id)
        appeal.created_at = old
        appeal.status = "in_progress"
        msg = await s.scalar(select(Message).where(Message.appeal_id == appeal.id))
        msg.created_at = old
        await s.flush()
        opid = op.id
    attempt_id = await attempts.reserve(
        appeal_id=db.appeal_id,
        operator_id=opid,
        source_key="unknown-retention",
        text="pending",
        attachments=[],
        is_final=True,
        part_plan=[[]],
    )
    await attempts.note_part(attempt_id, 0, "sending", None)
    await attempts.mark_unknown(attempt_id)
    async with db.scope() as s:
        await appeals.purge_old_appeals_content(s)
        appeal = await s.get(Appeal, db.appeal_id)
        attempt = await s.get(OperatorReplyAttempt, attempt_id)
        assert attempt is not None and appeal.content_erased_at is None


@pytest.mark.asyncio
async def test_old_inactive_unknown_attempt_does_not_block_retention_forever(
    erasure_db, monkeypatch
):
    db = erasure_db
    monkeypatch.setattr(attempts, "session_scope", db.scope)
    old = datetime.now(UTC) - timedelta(days=365 * 7)
    async with db.scope() as session:
        operator = Operator(max_user_id=90014, full_name="synthetic", role="it")
        session.add(operator)
        appeal = await session.get(Appeal, db.appeal_id)
        appeal.created_at = old
        appeal.status = "in_progress"
        message = await session.scalar(select(Message).where(Message.appeal_id == appeal.id))
        message.created_at = old
        await session.flush()
        operator_id = operator.id
    attempt_id = await attempts.reserve(
        appeal_id=db.appeal_id,
        operator_id=operator_id,
        source_key="old-unknown",
        text="pending",
        attachments=[],
        is_final=True,
        part_plan=[[]],
    )
    await attempts.note_part(attempt_id, 0, "sending", None)
    await attempts.mark_unknown(attempt_id)
    async with db.scope() as session:
        attempt = await session.get(OperatorReplyAttempt, attempt_id)
        attempt.created_at = old
        attempt.last_activity_at = old
    async with db.scope() as session:
        await appeals.purge_old_appeals_content(session)
        appeal = await session.get(Appeal, db.appeal_id)
        assert await session.get(OperatorReplyAttempt, attempt_id) is None
        assert appeal.content_erased_at is not None


@pytest.mark.asyncio
async def test_recent_full_reconciliation_protects_recorded_partial_activity(
    erasure_db, monkeypatch
):
    db = erasure_db
    monkeypatch.setattr(attempts, "session_scope", db.scope)
    old = datetime.now(UTC) - timedelta(days=365 * 7)
    async with db.scope() as session:
        operator = Operator(max_user_id=90015, full_name="synthetic", role="it")
        session.add(operator)
        appeal = await session.get(Appeal, db.appeal_id)
        appeal.created_at = old
        appeal.status = "in_progress"
        message = await session.scalar(select(Message).where(Message.appeal_id == appeal.id))
        message.created_at = old
        await session.flush()
        operator_id = operator.id
    files = [{"type": "file", "payload": {"token": name}} for name in ("one", "two")]
    attempt_id = await attempts.reserve(
        appeal_id=db.appeal_id,
        operator_id=operator_id,
        source_key="recorded-retention",
        text="Reply",
        attachments=files,
        is_final=True,
        part_plan=[[0], [1]],
    )
    await attempts.note_part(attempt_id, 0, "sending", None)
    await attempts.note_part(attempt_id, 0, "confirmed", "first-mid")
    await attempts.note_part(attempt_id, 1, "sending", None)
    await attempts.mark_unknown(attempt_id)
    async with db.scope() as session:
        attempt = await session.get(OperatorReplyAttempt, attempt_id)
        attempt.created_at = old
        parts = list(attempt.delivery_parts)
        parts[0] = {**parts[0], "confirmed_at": old.isoformat()}
        attempt.delivery_parts = parts
    async with db.scope() as session:
        assert await attempts.resolve_unknown(
            session, attempt_id, delivered=True, actor_max_id=90015
        )
    async with db.scope() as session:
        await appeals.purge_old_appeals_content(session)
        appeal = await session.get(Appeal, db.appeal_id)
        attempt = await session.get(OperatorReplyAttempt, attempt_id)
        assert appeal.summary == "Private" and appeal.content_erased_at is None
        assert attempt is not None and attempt.state == "recorded"


@pytest.mark.asyncio
async def test_reconciliation_prompt_holds_privacy_until_output(erasure_db, monkeypatch):
    import asyncio

    from aemr_bot.handlers import admin_reply_reconciliation
    from tests._helpers import make_event

    db = erasure_db
    monkeypatch.setattr(attempts, "session_scope", db.scope)
    monkeypatch.setattr(admin_reply_reconciliation, "session_scope", db.scope)
    monkeypatch.setattr(admin_reply_reconciliation, "ensure_operator", AsyncMock(return_value=True))
    monkeypatch.setattr(admin_reply_reconciliation, "ack_callback", AsyncMock())
    async with db.scope() as session:
        operator = Operator(max_user_id=90016, full_name="synthetic", role="it")
        session.add(operator)
        await session.flush()
        operator_id = operator.id
    files = [{"type": "file", "payload": {"token": "private"}}]
    attempt_id = await attempts.reserve(
        appeal_id=db.appeal_id,
        operator_id=operator_id,
        source_key="prompt-privacy",
        text="private",
        attachments=files,
        is_final=True,
    )
    await attempts.note_part(attempt_id, 0, "sending", None)
    await attempts.mark_unknown(attempt_id)
    output_started, release, erase_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def display(*args, **kwargs):
        output_started.set()
        await release.wait()

    monkeypatch.setattr(admin_reply_reconciliation, "send_or_edit_screen", display)
    event = make_event(chat_id=123, user_id=90016)
    view = asyncio.create_task(
        admin_reply_reconciliation._op_reply_resolve(
            event, f"op:reply_resolve:{attempt_id}:delivered"
        )
    )
    erase = None
    try:
        await asyncio.wait_for(output_started.wait(), 5)

        async def erase_resident():
            erase_started.set()
            async with db.scope() as session:
                await users.erase_pdn_detailed(session, 42)

        erase = asyncio.create_task(erase_resident())
        await asyncio.wait_for(erase_started.wait(), 5)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(erase), 0.2)
    finally:
        release.set()
        await asyncio.gather(view, *([erase] if erase else []))
    async with db.scope() as session:
        assert await session.get(OperatorReplyAttempt, attempt_id) is None


@pytest.mark.asyncio
async def test_unavailable_attempt_attachment_warning_is_inventoried(erasure_db, monkeypatch):
    from aemr_bot.db.models import ResidentDataCopy
    from aemr_bot.handlers import admin_reply_attachments
    from aemr_bot.services import admin_relay
    from tests._helpers import make_event

    db = erasure_db
    monkeypatch.setattr(attempts, "session_scope", db.scope)
    monkeypatch.setattr(admin_reply_attachments, "session_scope", db.scope)
    monkeypatch.setattr(admin_reply_attachments, "ensure_operator", AsyncMock(return_value=True))
    monkeypatch.setattr(admin_reply_attachments, "ack_callback", AsyncMock())
    monkeypatch.setattr(admin_relay, "deserialize_for_relay", lambda _: [])
    async with db.scope() as session:
        operator = Operator(max_user_id=90017, full_name="synthetic", role="it")
        session.add(operator)
        await session.flush()
        operator_id = operator.id
    files = [{"type": "file", "payload": {"token": "unavailable"}}]
    attempt_id = await attempts.reserve(
        appeal_id=db.appeal_id,
        operator_id=operator_id,
        source_key="warning-privacy",
        text="private",
        attachments=files,
        is_final=True,
    )
    await attempts.mark_unknown(attempt_id)
    event = make_event(chat_id=123, user_id=90017)
    event.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id="warning-mid"))
    data_erasure.install_outgoing_hook(event.bot)
    await admin_reply_attachments._op_reply_attempt_atts(event, attempt_id)
    async with db.scope() as session:
        copy = await session.scalar(
            select(ResidentDataCopy).where(ResidentDataCopy.message_id == "warning-mid")
        )
        assert copy is not None and data_erasure.subject_digest(42) in copy.subjects
        await users.erase_pdn_detailed(session, 42)
    async with db.scope() as session:
        copy = await session.scalar(
            select(ResidentDataCopy).where(ResidentDataCopy.message_id == "warning-mid")
        )
        assert copy.erase_requested and copy.state == "manual"


@pytest.mark.asyncio
async def test_reconciliation_prompt_copy_is_inventoried(erasure_db, monkeypatch):
    from aemr_bot.db.models import ResidentDataCopy
    from aemr_bot.handlers import admin_reply_reconciliation
    from tests._helpers import make_event

    db = erasure_db
    monkeypatch.setattr(attempts, "session_scope", db.scope)
    monkeypatch.setattr(admin_reply_reconciliation, "session_scope", db.scope)
    monkeypatch.setattr(admin_reply_reconciliation, "ensure_operator", AsyncMock(return_value=True))
    monkeypatch.setattr(admin_reply_reconciliation, "ack_callback", AsyncMock())
    async with db.scope() as session:
        operator = Operator(max_user_id=90018, full_name="synthetic", role="it")
        session.add(operator)
        await session.flush()
        operator_id = operator.id
    files = [{"type": "file", "payload": {"token": "private"}}]
    attempt_id = await attempts.reserve(
        appeal_id=db.appeal_id,
        operator_id=operator_id,
        source_key="prompt-inventory",
        text="private",
        attachments=files,
        is_final=True,
    )
    await attempts.note_part(attempt_id, 0, "sending", None)
    await attempts.mark_unknown(attempt_id)
    event = make_event(chat_id=123, user_id=90018)
    event.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id="prompt-mid"))
    data_erasure.install_outgoing_hook(event.bot)
    await admin_reply_reconciliation._op_reply_resolve(
        event, f"op:reply_resolve:{attempt_id}:delivered"
    )
    async with db.scope() as session:
        copy = await session.scalar(
            select(ResidentDataCopy).where(ResidentDataCopy.message_id == "prompt-mid")
        )
        assert copy is not None and data_erasure.subject_digest(42) in copy.subjects
        await users.erase_pdn_detailed(session, 42)
    async with db.scope() as session:
        copy = await session.scalar(
            select(ResidentDataCopy).where(ResidentDataCopy.message_id == "prompt-mid")
        )
        assert copy.erase_requested and copy.state == "manual"
