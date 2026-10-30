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
async def test_uncertain_attempt_is_not_replayed_and_erasure_removes_content(session, monkeypatch, tmp_path):
    from aemr_bot.services import data_erasure

    monkeypatch.setattr(data_erasure.settings, "erasure_state_dir", str(tmp_path / "erasure"))
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


@pytest.mark.asyncio
async def test_failed_early_recovery_does_not_block_later_attempts(session, monkeypatch):
    appeal, operator = await _rows(session, 82050)
    ids = []
    for index in range(2):
        attempt_id = await operator_reply_attempts.reserve(
            appeal_id=appeal.id, operator_id=operator.id, source_key=f"fair-recovery-{index}",
            text=f"Ответ {index}", attachments=[], is_final=False,
        )
        await operator_reply_attempts.mark_confirmed(attempt_id, f"fair-mid-{index}")
        ids.append(attempt_id)
    monkeypatch.setattr(operator_reply_attempts, "_recovery_cursor", 0)
    original = operator_reply_attempts.record_confirmed

    async def fail_first(db_session, attempt_id, **kwargs):
        if attempt_id == ids[0]:
            raise RuntimeError("simulated local failure")
        return await original(db_session, attempt_id, **kwargs)

    monkeypatch.setattr(operator_reply_attempts, "record_confirmed", fail_first)
    assert await operator_reply_attempts.recover_confirmed(limit=1) == 0
    assert await operator_reply_attempts.recover_confirmed(limit=1) == 1
    assert await session.scalar(select(Message.id).where(Message.max_message_id == "fair-mid-1"))


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["confirmed", "unknown"])
async def test_handler_delivery_reserves_before_max_and_never_replays_same_event(
    session, monkeypatch, outcome,
):
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from aemr_bot.handlers import operator_reply
    from aemr_bot.services import appeals
    from tests._helpers import make_event

    appeal, operator = await _rows(session, 89300)
    resident = await session.get(User, appeal.user_id)
    resident.consent_pdn_at = datetime.now(UTC)
    await session.commit()
    appeal = await appeals.get_by_id(session, appeal.id)
    event = make_event(chat_id=123, user_id=operator.max_user_id)
    event.message.link = None
    citizen_calls = []

    async def send(**kwargs):
        if kwargs.get("user_id") is not None:
            # Durable reservation must already be committed before MAX starts.
            async with operator_reply.session_scope() as check:
                attempt = await check.scalar(select(OperatorReplyAttempt))
                assert attempt is not None and attempt.state == "sending"
                assert attempt.delivery_parts == [{"index": 0, "state": "sending", "mid": None}]
            citizen_calls.append(kwargs)
            if outcome == "unknown":
                raise TimeoutError("MAX outcome unknown")
        return SimpleNamespace(body=SimpleNamespace(mid="confirmed-mid"))

    event.bot.send_message = AsyncMock(side_effect=send)
    monkeypatch.setattr("aemr_bot.services.admin_card.render", AsyncMock())
    operator_reply._recent_replies.clear()
    for _ in range(2):
        assert await operator_reply._deliver_operator_reply(
            event, appeal=appeal, operator=operator, text="Официальный ответ",
            audit_action="reply_via_command", is_final=False,
        )
    assert len(citizen_calls) == 1
    attempt = await session.scalar(select(OperatorReplyAttempt))
    assert attempt.state == ("recorded" if outcome == "confirmed" else "unknown")
    messages = list(await session.scalars(select(Message).where(Message.appeal_id == appeal.id)))
    assert len(messages) == (1 if outcome == "confirmed" else 0)
