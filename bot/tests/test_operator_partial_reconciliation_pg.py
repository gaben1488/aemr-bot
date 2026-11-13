"""Reconcile one uncertain transport part without inventing later delivery."""

import pytest
from sqlalchemy import select

from aemr_bot.db.models import Appeal, Message, Operator, OperatorReplyAttempt, User
from aemr_bot.services import operator_reply_attempts as attempts


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain_delivered", [False, True])
async def test_partial_delivery_records_only_verified_parts_and_keeps_remainder(
    session, uncertain_delivered
):
    user = User(max_user_id=90501)
    operator = Operator(max_user_id=90502, full_name="Operator", role="aemr")
    session.add_all([user, operator])
    await session.flush()
    appeal = Appeal(user_id=user.id, status="new", summary="Test", attachments=[])
    session.add(appeal)
    await session.commit()
    files = [{"type": "file", "payload": {"token": token}} for token in ("one", "two", "three")]
    attempt_id = await attempts.reserve(
        appeal_id=appeal.id,
        operator_id=operator.id,
        source_key="partial-test",
        text="Reply",
        attachments=files,
        is_final=True,
        part_plan=[[0], [1], [2]],
    )
    await attempts.note_part(attempt_id, 0, "sending", None)
    await attempts.note_part(attempt_id, 0, "confirmed", "first-mid")
    await attempts.note_part(attempt_id, 1, "sending", None)
    await attempts.mark_unknown(attempt_id)
    assert await attempts.resolve_unknown(
        session, attempt_id, delivered=uncertain_delivered, actor_max_id=operator.max_user_id
    )
    await session.flush()
    messages = list(await session.scalars(select(Message).where(Message.appeal_id == appeal.id)))
    assert len(messages) == 1
    assert messages[0].text == "Reply" and messages[0].response_kind == "intermediate"
    assert messages[0].attachments == files[: 2 if uncertain_delivered else 1]
    assert messages[0].max_message_id == "first-mid"
    assert messages[0].delivery_source == ("manual_mixed" if uncertain_delivered else "max")
    from aemr_bot.services.message_labels import response_label
    assert ("подтверждена сотрудником" in response_label(messages[0])) is uncertain_delivered
    await session.refresh(appeal)
    assert appeal.status == "in_progress"
    attempt = await session.get(OperatorReplyAttempt, attempt_id)
    assert attempt.state == "not_delivered" and attempt.text is None
    assert attempt.attachments == files[2 if uncertain_delivered else 1 :]
    assert not await attempts.resolve_unknown(
        session, attempt_id, delivered=uncertain_delivered, actor_max_id=operator.max_user_id
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("delivered", [False, True])
async def test_first_part_uncertain_preserves_only_undelivered_content(session, delivered):
    user = User(max_user_id=90511)
    operator = Operator(max_user_id=90512, full_name="Operator", role="aemr")
    session.add_all([user, operator])
    await session.flush()
    appeal = Appeal(user_id=user.id, status="new", summary="Test", attachments=[])
    session.add(appeal)
    await session.commit()
    files = [{"type": "file", "payload": {"token": token}} for token in ("one", "two")]
    attempt_id = await attempts.reserve(
        appeal_id=appeal.id,
        operator_id=operator.id,
        source_key="first-part",
        text="Reply",
        attachments=files,
        is_final=True,
        part_plan=[[0], [1]],
    )
    await attempts.note_part(attempt_id, 0, "sending", None)
    await attempts.mark_unknown(attempt_id)
    assert await attempts.resolve_unknown(
        session, attempt_id, delivered=delivered, actor_max_id=operator.max_user_id
    )
    await session.flush()
    messages = list(await session.scalars(select(Message).where(Message.appeal_id == appeal.id)))
    attempt = await session.get(OperatorReplyAttempt, attempt_id)
    if delivered:
        assert len(messages) == 1 and messages[0].attachments == files[:1]
        assert messages[0].text == "Reply" and messages[0].response_kind == "intermediate"
        assert messages[0].max_message_id is None and attempt.text is None
        assert messages[0].delivery_uncertain_until is not None
        assert attempt.attachments == files[1:]
    else:
        assert not messages and attempt.text == "Reply" and attempt.attachments == files
    assert attempt.state == "not_delivered"


@pytest.mark.asyncio
async def test_all_confirmed_parts_recover_without_resending(session, monkeypatch):
    user = User(max_user_id=90521)
    operator = Operator(max_user_id=90522, full_name="Operator", role="aemr")
    session.add_all([user, operator])
    await session.flush()
    appeal = Appeal(user_id=user.id, status="new", summary="Test", attachments=[])
    session.add(appeal)
    await session.commit()
    files = [{"type": "file", "payload": {"token": token}} for token in ("one", "two")]
    attempt_id = await attempts.reserve(
        appeal_id=appeal.id,
        operator_id=operator.id,
        source_key="all-confirmed",
        text="Reply",
        attachments=files,
        is_final=True,
        part_plan=[[0], [1]],
    )
    for index in range(2):
        await attempts.note_part(attempt_id, index, "sending", None)
        await attempts.note_part(attempt_id, index, "confirmed", f"mid-{index}")
    monkeypatch.setattr(attempts, "_recovery_cursor", 0)
    assert await attempts.recover_confirmed() == 1
    assert await attempts.recover_confirmed() == 0
    messages = list(await session.scalars(select(Message).where(Message.appeal_id == appeal.id)))
    assert len(messages) == 1 and messages[0].attachments == files
    assert messages[0].response_kind == "final" and messages[0].max_message_id == "mid-0"
    await session.refresh(appeal)
    assert appeal.status == "answered"


@pytest.mark.asyncio
@pytest.mark.parametrize("delivered", [False, True])
async def test_legacy_multiple_files_can_be_reconciled_explicitly(session, delivered):
    user = User(max_user_id=90531)
    operator = Operator(max_user_id=90532, full_name="Operator", role="aemr")
    session.add_all([user, operator])
    await session.flush()
    appeal = Appeal(user_id=user.id, status="new", summary="Test", attachments=[])
    session.add(appeal)
    await session.commit()
    files = [{"type": "file", "payload": {"token": value}} for value in ("old-1", "old-2")]
    attempt_id = await attempts.reserve(
        appeal_id=appeal.id,
        operator_id=operator.id,
        source_key="legacy-part",
        text="Reply",
        attachments=files,
        is_final=True,
    )
    await attempts.note_part(attempt_id, 0, "sending", None)
    await attempts.mark_unknown(attempt_id)
    assert await attempts.resolve_unknown(
        session, attempt_id, delivered=delivered, actor_max_id=operator.max_user_id
    )
    attempt = await session.get(OperatorReplyAttempt, attempt_id)
    message = await session.scalar(select(Message).where(Message.appeal_id == appeal.id))
    if delivered:
        assert attempt.state == "recorded" and message.attachments == files
        assert message.max_message_id is None and message.delivery_source == "manual_partial"
        assert all("attachments" not in part for part in attempt.delivery_parts)
    else:
        assert message is None and attempt.state == "not_delivered" and attempt.attachments == files


@pytest.mark.asyncio
async def test_legacy_confirmed_part_cannot_be_marked_all_not_delivered(session):
    user = User(max_user_id=90533)
    operator = Operator(max_user_id=90534, full_name="Operator", role="aemr")
    session.add_all([user, operator])
    await session.flush()
    appeal = Appeal(user_id=user.id, status="new", summary="Test", attachments=[])
    session.add(appeal)
    await session.commit()
    files = [{"type": "file", "payload": {"token": value}} for value in ("one", "two")]
    attempt_id = await attempts.reserve(appeal_id=appeal.id, operator_id=operator.id,
        source_key="old-confirmed", text="Reply", attachments=files, is_final=True)
    await attempts.note_part(attempt_id, 0, "sending", None)
    await attempts.note_part(attempt_id, 0, "confirmed", "first-mid")
    await attempts.note_part(attempt_id, 1, "sending", None)
    await attempts.mark_unknown(attempt_id)
    with pytest.raises(ValueError, match="без плана"):
        await attempts.resolve_unknown(session, attempt_id, delivered=False, actor_max_id=operator.max_user_id)
    attempt = await session.get(OperatorReplyAttempt, attempt_id)
    assert attempt.state == "unknown" and attempt.attachments == files


@pytest.mark.asyncio
@pytest.mark.parametrize("delivered", [False, True])
async def test_legacy_single_file_can_be_reconciled(session, delivered):
    user = User(max_user_id=90541)
    operator = Operator(max_user_id=90542, full_name="Operator", role="aemr")
    session.add_all([user, operator])
    await session.flush()
    appeal = Appeal(user_id=user.id, status="new", summary="Test", attachments=[])
    session.add(appeal)
    await session.commit()
    files = [{"type": "file", "payload": {"token": "old"}}]
    attempt_id = await attempts.reserve(appeal_id=appeal.id, operator_id=operator.id,
        source_key="old-single", text="Reply", attachments=files, is_final=True)
    await attempts.note_part(attempt_id, 0, "sending", None)
    await attempts.mark_unknown(attempt_id)
    assert await attempts.resolve_unknown(session, attempt_id, delivered=delivered,
                                          actor_max_id=operator.max_user_id)
    await session.flush()
    messages = list(await session.scalars(select(Message).where(Message.appeal_id == appeal.id)))
    attempt = await session.get(OperatorReplyAttempt, attempt_id)
    if delivered:
        assert len(messages) == 1 and messages[0].attachments == files
    else:
        assert not messages and attempt.attachments == files


@pytest.mark.asyncio
async def test_partial_recovery_does_not_move_confirmed_reply_to_new_period(session):
    from datetime import UTC, datetime, timedelta

    from aemr_bot.services import appeals

    user = User(max_user_id=90551)
    operator = Operator(max_user_id=90552, full_name="Operator", role="aemr")
    session.add_all([user, operator])
    await session.flush()
    appeal = Appeal(user_id=user.id, status="new", summary="Test", attachments=[])
    session.add(appeal)
    await session.commit()
    files = [{"type": "file", "payload": {"token": name}} for name in ("first", "second")]
    attempt_id = await attempts.reserve(appeal_id=appeal.id, operator_id=operator.id,
        source_key="new-period", text="Reply", attachments=files, is_final=True,
        part_plan=[[0], [1]])
    await attempts.note_part(attempt_id, 0, "sending", None)
    await attempts.note_part(attempt_id, 0, "confirmed", "first-mid")
    await attempts.note_part(attempt_id, 1, "sending", None)
    await attempts.mark_unknown(attempt_id)
    appeal.sla_anchor_at = datetime.now(UTC) + timedelta(minutes=1)
    await session.commit()
    assert await attempts.resolve_unknown(session, attempt_id, delivered=False,
                                          actor_max_id=operator.max_user_id)
    await session.flush()
    message = await session.scalar(select(Message).where(Message.appeal_id == appeal.id))
    assert message.created_at < appeal.sla_anchor_at
    assert appeal.id not in await appeals.first_operator_reply_times(session, [appeal.id])
    assert appeal.status == "new"


@pytest.mark.asyncio
async def test_real_delivery_recovers_after_final_part_before_history(session, monkeypatch):
    import asyncio
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from aemr_bot.handlers import operator_reply
    from aemr_bot.services import appeals
    from tests._helpers import make_event

    user = User(max_user_id=90561, consent_pdn_at=datetime.now(UTC))
    operator = Operator(max_user_id=90562, full_name="Operator", role="aemr")
    session.add_all([user, operator])
    await session.flush()
    appeal = Appeal(user_id=user.id, status="new", summary="Test", attachments=[])
    session.add(appeal)
    await session.commit()
    appeal = await appeals.get_by_id(session, appeal.id)
    files = [{"type": "file", "payload": {"token": name}} for name in ("one", "two")]
    monkeypatch.setattr(operator_reply._image_attachments, "deserialize_for_relay",
                        lambda attachments: [SimpleNamespace(type="file") for _ in attachments])
    monkeypatch.setattr(operator_reply, "_persist_reply_and_card", AsyncMock(side_effect=asyncio.CancelledError))
    event = make_event(chat_id=123, user_id=operator.max_user_id)
    event.bot.send_message = AsyncMock(side_effect=[
        SimpleNamespace(body=SimpleNamespace(mid="file-one")),
        SimpleNamespace(body=SimpleNamespace(mid="file-two")),
    ])
    with pytest.raises(asyncio.CancelledError):
        await operator_reply._deliver_operator_reply(event, appeal=appeal, operator=operator,
            text="Reply", audit_action="reply_via_draft", is_final=True,
            composition={"token": "crash-after-final", "attachments": files})
    async with operator_reply.session_scope() as check:
        attempt = await check.scalar(select(OperatorReplyAttempt))
        assert attempt.state == "confirmed"
        assert [part["attachments"] for part in attempt.delivery_parts] == [[0], [1]]
        assert [part["mid"] for part in attempt.delivery_parts] == ["file-one", "file-two"]
    monkeypatch.setattr(attempts, "_recovery_cursor", 0)
    assert await attempts.recover_confirmed() == 1
    assert await attempts.recover_confirmed() == 0
    messages = list(await session.scalars(select(Message).where(Message.appeal_id == appeal.id)))
    assert len(messages) == 1 and messages[0].attachments == files
    assert messages[0].max_message_id == "file-one"
    assert event.bot.send_message.await_count == 2
