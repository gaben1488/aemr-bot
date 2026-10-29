"""Reply attempts preserve delivery evidence without replaying MAX requests."""
import pytest
from sqlalchemy import select

from aemr_bot.db.models import Appeal, Message, Operator, OperatorReplyAttempt, User
from aemr_bot.services import operator_reply_attempts, users


async def _rows(session, ident):
    user = User(max_user_id=ident)
    operator = Operator(max_user_id=ident + 1, full_name="Специалист", role="aemr")
    session.add_all([user, operator])
    await session.flush()
    appeal = Appeal(user_id=user.id, status="in_progress", summary="Обращение", attachments=[])
    session.add(appeal)
    await session.commit()
    return appeal, operator


@pytest.mark.asyncio
async def test_confirmed_attempt_recovers_history_once_without_sending(session):
    appeal, operator = await _rows(session, 82000)
    attempt_id = await operator_reply_attempts.reserve(
        appeal_id=appeal.id, operator_id=operator.id, source_key="reply-event-1",
        text="Подготовлен ответ", attachments=[], is_final=False,
    )
    assert attempt_id
    await operator_reply_attempts.mark_confirmed(attempt_id, "delivered-mid")
    assert await operator_reply_attempts.recover_confirmed() == 1
    assert await operator_reply_attempts.recover_confirmed() == 0
    messages = list(await session.scalars(select(Message).where(Message.appeal_id == appeal.id)))
    assert len(messages) == 1
    assert (messages[0].text, messages[0].response_kind, messages[0].max_message_id) == (
        "Подготовлен ответ", "intermediate", "delivered-mid"
    )
    row = await session.get(OperatorReplyAttempt, attempt_id)
    assert row.state == "recorded" and row.text is None and row.attachments == []
    assert await operator_reply_attempts.reserve(
        appeal_id=appeal.id, operator_id=operator.id, source_key="reply-event-1",
        text="Подготовлен ответ", attachments=[], is_final=False,
    ) is None


@pytest.mark.asyncio
async def test_uncertain_attempt_is_not_replayed_and_erasure_removes_content(session):
    appeal, operator = await _rows(session, 82010)
    attempt_id = await operator_reply_attempts.reserve(
        appeal_id=appeal.id, operator_id=operator.id, source_key="reply-event-2",
        text="Ответ с персональными данными", attachments=[], is_final=True,
    )
    await operator_reply_attempts.mark_unknown(attempt_id)
    assert await operator_reply_attempts.recover_confirmed() == 0
    attempt = await session.get(OperatorReplyAttempt, attempt_id)
    assert attempt.state == "unknown" and attempt.text == "Ответ с персональными данными"
    assert await users.erase_pdn(session, 82010)
    assert await session.scalar(select(OperatorReplyAttempt.id).where(OperatorReplyAttempt.id == attempt_id)) is None


@pytest.mark.asyncio
async def test_human_reconciliation_never_fabricates_platform_id(session):
    appeal, operator = await _rows(session, 82020)
    attempt_id = await operator_reply_attempts.reserve(
        appeal_id=appeal.id, operator_id=operator.id, source_key="reply-event-3",
        text="Ответ", attachments=[], is_final=True,
    )
    await operator_reply_attempts.mark_unknown(attempt_id)
    assert await operator_reply_attempts.resolve_unknown(
        session, attempt_id, delivered=True, actor_max_id=operator.max_user_id,
    )
    messages = list(await session.scalars(select(Message).where(Message.appeal_id == appeal.id)))
    assert len(messages) == 1 and messages[0].text == "Ответ"
    assert messages[0].max_message_id is None
    assert messages[0].delivery_source == "manual"
    assert not await operator_reply_attempts.resolve_unknown(
        session, attempt_id, delivered=True, actor_max_id=operator.max_user_id,
    )


@pytest.mark.asyncio
async def test_not_delivered_draft_is_preserved_until_explicit_discard(session):
    appeal, operator = await _rows(session, 82030)
    attempt_id = await operator_reply_attempts.reserve(
        appeal_id=appeal.id, operator_id=operator.id, source_key="reply-event-4",
        text="Черновик", attachments=[], is_final=False,
    )
    await operator_reply_attempts.mark_unknown(attempt_id)
    assert await operator_reply_attempts.resolve_unknown(
        session, attempt_id, delivered=False, actor_max_id=operator.max_user_id,
    )
    row = await session.get(OperatorReplyAttempt, attempt_id)
    assert row.state == "not_delivered" and row.text == "Черновик"
    assert await operator_reply_attempts.discard_not_delivered(session, attempt_id, actor_max_id=operator.max_user_id)
    assert row.state == "discarded" and row.text is None


@pytest.mark.asyncio
async def test_late_reply_recovery_does_not_close_newer_followup(session):
    from datetime import UTC, datetime, timedelta

    appeal, operator = await _rows(session, 82040)
    attempt_id = await operator_reply_attempts.reserve(
        appeal_id=appeal.id, operator_id=operator.id, source_key="reply-event-5",
        text="Старый итоговый ответ", attachments=[], is_final=True,
    )
    await operator_reply_attempts.mark_confirmed(attempt_id, "delivered-mid-5")
    # A newer resident message has already arrived while history recovery
    # was delayed. Do not close that newer work on the strength of an old reply.
    session.add(Message(appeal_id=appeal.id, direction="from_user", text="Новая информация",
                        attachments=[], created_at=datetime.now(UTC) + timedelta(seconds=1)))
    await session.commit()
    assert await operator_reply_attempts.recover_confirmed() == 1
    await session.refresh(appeal)
    assert appeal.status == "in_progress"
    rows = list(await session.scalars(select(Message).where(Message.appeal_id == appeal.id)))
    assert {m.text for m in rows} == {"Старый итоговый ответ", "Новая информация"}
