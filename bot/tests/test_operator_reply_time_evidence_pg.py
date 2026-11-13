"""Uncertain reply time and reconciliation confirmation regressions."""
import pytest
from sqlalchemy import select

from aemr_bot.db.models import Appeal, Message, Operator, OperatorReplyAttempt, User
from aemr_bot.services import operator_reply_attempts as attempts


def test_legacy_manual_label_distinguishes_confirmation_from_delivery_time():
    from types import SimpleNamespace

    from aemr_bot.services.message_labels import response_label

    label = response_label(SimpleNamespace(response_kind="final", delivery_source="manual"))
    assert "время подтверждения" in label and "время доставки неизвестно" in label


@pytest.mark.asyncio
async def test_uncertain_first_reply_is_not_a_fabricated_sla_time(session):
    from datetime import UTC, datetime, timedelta
    from io import BytesIO

    from openpyxl import load_workbook

    from aemr_bot.services import appeals, complete_stats, queue_panel, stats

    user = User(max_user_id=90571)
    operator = Operator(max_user_id=90572, full_name="Operator", role="aemr")
    session.add_all([user, operator])
    await session.flush()
    appeal = Appeal(user_id=user.id, status="new", summary="Test", attachments=[])
    session.add(appeal)
    await session.commit()
    attempt_id = await attempts.reserve(appeal_id=appeal.id, operator_id=operator.id,
        source_key="ambiguous-period", text="Reply", attachments=[], is_final=True, part_plan=[[]])
    await attempts.note_part(attempt_id, 0, "sending", None)
    await attempts.mark_unknown(attempt_id)
    attempt = await session.get(OperatorReplyAttempt, attempt_id)
    attempt.created_at = datetime.now(UTC) - timedelta(minutes=2)
    appeal.sla_anchor_at = datetime.now(UTC) - timedelta(minutes=1)
    await session.commit()
    assert await attempts.resolve_unknown(session, attempt_id, delivered=True, actor_max_id=operator.max_user_id)
    await session.commit()
    message = await session.scalar(select(Message).where(Message.appeal_id == appeal.id))
    assert message.created_at < appeal.sla_anchor_at < message.delivery_uncertain_until
    assert (await appeals.first_operator_reply_times(session, [appeal.id])) == {appeal.id: None}
    assert (await appeals.latest_intermediate_reply_times(session, [appeal.id])) == {appeal.id: None}
    assert appeal not in await appeals.find_overdue_unanswered(session, 1)
    text, _ = await queue_panel._snapshot()
    assert "Время ответа требует сверки" in text
    await session.refresh(appeal, ["user"])
    workbook = load_workbook(BytesIO(stats._render_workbook([appeal], {}, {appeal.id: None})))
    assert "Время или период ответа неизвестен" in [cell.value for cell in workbook.active[2]]
    data, _, _ = await complete_stats.build_xlsx(session, "all")
    full = load_workbook(BytesIO(data), read_only=True, data_only=True)
    summary = {row[0]: row[1] for row in list(full["Сводка"].values)[1:]}
    assert summary["Время или период первого ответа неизвестен"] == 1
    assert summary["Обращений с первым ответом в текущем периоде"] == 0


@pytest.mark.asyncio
async def test_recent_manual_confirmation_protects_old_appeal_from_retention(session):
    from datetime import UTC, datetime, timedelta

    from aemr_bot.services import appeals, card_format

    old = datetime.now(UTC) - timedelta(days=365 * 7)
    user = User(max_user_id=90581)
    operator = Operator(max_user_id=90582, full_name="Operator", role="aemr")
    session.add_all([user, operator])
    await session.flush()
    appeal = Appeal(user_id=user.id, status="new", summary="Keep", attachments=[], created_at=old)
    session.add(appeal)
    await session.commit()
    attempt_id = await attempts.reserve(appeal_id=appeal.id, operator_id=operator.id,
        source_key="retention-uncertain", text="Reply", attachments=[], is_final=True, part_plan=[[]])
    await attempts.note_part(attempt_id, 0, "sending", None)
    await attempts.mark_unknown(attempt_id)
    attempt = await session.get(OperatorReplyAttempt, attempt_id)
    attempt.created_at = old
    await session.commit()
    assert await attempts.resolve_unknown(session, attempt_id, delivered=True, actor_max_id=operator.max_user_id)
    await session.commit()
    await session.refresh(appeal)
    assert appeal.status == "answered" and appeal.answered_at is None
    assert "дата ответа неизвестна" in card_format.appeal_list_label(appeal)
    await appeals.purge_old_appeals_content(session, years=5)
    await session.refresh(appeal)
    assert appeal.summary == "Keep" and appeal.content_erased_at is None


@pytest.mark.asyncio
async def test_stale_confirmation_cannot_resolve_the_next_part(session):
    user = User(max_user_id=90591)
    operator = Operator(max_user_id=90592, full_name="Operator", role="aemr")
    session.add_all([user, operator])
    await session.flush()
    appeal = Appeal(user_id=user.id, status="new", summary="Test", attachments=[])
    session.add(appeal)
    await session.commit()
    files = [{"type": "file", "payload": {"token": name}} for name in ("one", "two")]
    attempt_id = await attempts.reserve(appeal_id=appeal.id, operator_id=operator.id,
        source_key="stale-confirmation", text="Reply", attachments=files, is_final=True,
        part_plan=[[0], [1]])
    await attempts.note_part(attempt_id, 0, "sending", None)
    async with attempts.session_scope() as check:
        old_revision = attempts.reconciliation_revision(await check.get(OperatorReplyAttempt, attempt_id))
    await attempts.note_part(attempt_id, 0, "confirmed", "mid-one")
    await attempts.note_part(attempt_id, 1, "sending", None)
    await attempts.mark_unknown(attempt_id)
    assert not await attempts.resolve_unknown(session, attempt_id, delivered=True,
        actor_max_id=operator.max_user_id, expected_revision=old_revision)
    assert await session.scalar(select(Message.id).where(Message.appeal_id == appeal.id)) is None
    attempt = await session.get(OperatorReplyAttempt, attempt_id)
    assert attempt.state == "unknown" and attempt.attachments == files
    revision = attempts.reconciliation_revision(attempt)
    assert await attempts.resolve_unknown(session, attempt_id, delivered=False,
        actor_max_id=operator.max_user_id, expected_revision=revision)


@pytest.mark.asyncio
async def test_unknown_latest_reply_is_visible_and_later_exact_reply_restores_timer(session):
    from datetime import UTC, datetime, timedelta

    from aemr_bot.services import appeals, queue_panel

    now = datetime.now(UTC)
    user = User(max_user_id=90601)
    session.add(user)
    await session.flush()
    appeal = Appeal(user_id=user.id, status="in_progress", summary="Test", attachments=[],
                    created_at=now - timedelta(hours=1))
    session.add(appeal)
    await session.flush()
    session.add_all([
        Message(appeal_id=appeal.id, direction="from_operator", response_kind="intermediate",
                text="Known", attachments=[], created_at=now - timedelta(minutes=20)),
        Message(appeal_id=appeal.id, direction="from_operator", response_kind="intermediate",
                text="Uncertain", attachments=[], created_at=now - timedelta(minutes=10),
                delivery_source="manual_partial", delivery_uncertain_until=now - timedelta(minutes=5)),
    ])
    await session.commit()
    assert (await appeals.first_operator_reply_times(session, [appeal.id]))[appeal.id] == now - timedelta(minutes=20)
    assert (await appeals.latest_intermediate_reply_times(session, [appeal.id])) == {appeal.id: None}
    text, _ = await queue_panel._snapshot()
    assert "Время ответа требует сверки" in text
    session.add(Message(appeal_id=appeal.id, direction="from_operator", response_kind="intermediate",
                        text="Later", attachments=[], created_at=now))
    await session.commit()
    assert (await appeals.latest_intermediate_reply_times(session, [appeal.id])) == {appeal.id: now}


@pytest.mark.asyncio
async def test_xlsx_last_reply_marks_overlapping_delivery_interval(session):
    from datetime import UTC, datetime, timedelta

    from aemr_bot.services import stats

    now = datetime.now(UTC)
    user = User(max_user_id=90603)
    session.add(user)
    await session.flush()
    appeal = Appeal(user_id=user.id, status="in_progress", summary="Test", attachments=[])
    session.add(appeal)
    await session.flush()
    session.add_all([
        Message(appeal_id=appeal.id, direction="from_operator", text="Uncertain", attachments=[],
                created_at=now - timedelta(minutes=10), delivery_source="manual_partial",
                delivery_uncertain_until=now + timedelta(minutes=5)),
        Message(appeal_id=appeal.id, direction="from_operator", text="Known", attachments=[],
                created_at=now),
    ])
    await session.flush()
    result = await stats._load_last_operator_replies(session, [appeal.id])
    assert "Порядок ответов требует сверки" in result[appeal.id] and "Known" in result[appeal.id]
    session.add(Message(appeal_id=appeal.id, direction="from_operator", text="Definitely last",
                        attachments=[], created_at=now + timedelta(minutes=10)))
    await session.flush()
    assert (await stats._load_last_operator_replies(session, [appeal.id]))[appeal.id] == "Definitely last"


@pytest.mark.asyncio
async def test_stale_prompt_handler_does_not_confirm_a_new_part(session, monkeypatch):
    from unittest.mock import AsyncMock

    from aemr_bot.handlers import admin_reply_reconciliation
    from tests._helpers import make_event

    user = User(max_user_id=90611)
    operator = Operator(max_user_id=90612, full_name="Operator", role="aemr")
    session.add_all([user, operator])
    await session.flush()
    appeal = Appeal(user_id=user.id, status="new", summary="Test", attachments=[])
    session.add(appeal)
    await session.commit()
    files = [{"type": "file", "payload": {"token": value}} for value in ("one", "two")]
    attempt_id = await attempts.reserve(appeal_id=appeal.id, operator_id=operator.id,
        source_key="stale-prompt", text="Reply", attachments=files, is_final=True, part_plan=[[0], [1]])
    await attempts.note_part(attempt_id, 0, "sending", None)
    event = make_event(chat_id=123, user_id=operator.max_user_id)
    monkeypatch.setattr(admin_reply_reconciliation, "ensure_operator", AsyncMock(return_value=True))
    monkeypatch.setattr(admin_reply_reconciliation, "ack_callback", AsyncMock())
    display = AsyncMock()
    monkeypatch.setattr(admin_reply_reconciliation, "send_or_edit_screen", display)
    await admin_reply_reconciliation._op_reply_resolve(event, f"op:reply_resolve:{attempt_id}:delivered")
    old_payload = display.await_args.kwargs["attachments"][0].model_dump()["payload"]["buttons"][0][0]["payload"]
    assert old_payload.count(":") == 4
    await attempts.note_part(attempt_id, 0, "confirmed", "mid-one")
    await attempts.note_part(attempt_id, 1, "sending", None)
    await attempts.mark_unknown(attempt_id)
    await admin_reply_reconciliation._op_reply_resolve(event, old_payload)
    assert "уже изменилось" in display.await_args.kwargs["text"]
    async with attempts.session_scope() as check:
        attempt = await check.get(OperatorReplyAttempt, attempt_id)
        assert attempt.state == "unknown" and attempt.attachments == files
        assert await check.scalar(select(Message.id).where(Message.appeal_id == appeal.id)) is None


@pytest.mark.asyncio
async def test_legacy_confirmation_demands_every_file_and_rejects_stale_prompt(session, monkeypatch):
    from unittest.mock import AsyncMock

    from aemr_bot.handlers import admin_reply_reconciliation
    from tests._helpers import make_event

    user = User(max_user_id=90621)
    operator = Operator(max_user_id=90622, full_name="Operator", role="aemr")
    session.add_all([user, operator])
    await session.flush()
    appeal = Appeal(user_id=user.id, status="new", summary="Test", attachments=[])
    session.add(appeal)
    await session.commit()
    files = [{"type": "file", "payload": {"token": value}} for value in ("one", "two")]
    attempt_id = await attempts.reserve(appeal_id=appeal.id, operator_id=operator.id,
        source_key="legacy-prompt", text="Reply", attachments=files, is_final=True)
    await attempts.note_part(attempt_id, 0, "sending", None)
    event = make_event(chat_id=123, user_id=operator.max_user_id)
    monkeypatch.setattr(admin_reply_reconciliation, "ensure_operator", AsyncMock(return_value=True))
    monkeypatch.setattr(admin_reply_reconciliation, "ack_callback", AsyncMock())
    display = AsyncMock()
    monkeypatch.setattr(admin_reply_reconciliation, "send_or_edit_screen", display)
    await admin_reply_reconciliation._op_reply_resolve(event, f"op:reply_resolve:{attempt_id}:delivered")
    assert "весь текст и каждое из 2 вложений" in display.await_args.kwargs["text"]
    old_payload = display.await_args.kwargs["attachments"][0].model_dump()["payload"]["buttons"][0][0]["payload"]
    await attempts.note_part(attempt_id, 0, "confirmed", "mid-one")
    await attempts.note_part(attempt_id, 1, "sending", None)
    await attempts.mark_unknown(attempt_id)
    await admin_reply_reconciliation._op_reply_resolve(event, old_payload)
    assert "уже изменилось" in display.await_args.kwargs["text"]
    async with attempts.session_scope() as check:
        attempt = await check.get(OperatorReplyAttempt, attempt_id)
        assert attempt.state == "unknown" and attempt.attachments == files
        assert await check.scalar(select(Message.id).where(Message.appeal_id == appeal.id)) is None
