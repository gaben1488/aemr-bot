"""A new period must not erase a known missed deadline in the closed period."""
from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from openpyxl import load_workbook

from aemr_bot.db.models import Message, Operator, OperatorReplyAttempt, User
from aemr_bot.services import appeals, complete_stats, operator_reply_attempts, settings_store


@pytest.mark.asyncio
@pytest.mark.parametrize("hours,overdue", [(1, False), (6, True)])
async def test_reopen_preserves_unanswered_closed_period_assessment(session, hours, overdue):
    user = User(max_user_id=919991)
    session.add(user)
    await session.flush()
    await settings_store.set_value(session, "sla_response_hours", 2)
    appeal = await appeals.create_appeal(session, user, "Адрес", "Тема", "Текст", [])
    start = datetime(2026, 10, 27, 21, tzinfo=UTC)
    appeal.created_at = appeal.sla_anchor_at = start
    assert await appeals.close(session, appeal.id)
    appeal.closed_at = start + timedelta(hours=hours)
    old_end = appeal.closed_at
    await settings_store.set_value(session, "sla_response_hours", 8)
    await session.flush()
    assert await appeals.reopen(session, appeal.id) == "reopened"
    await session.refresh(appeal)
    prior = appeal.sla_history[-1]
    assert prior["overdue"] is overdue
    assert prior["first_response"] is None
    assert datetime.fromisoformat(prior["end"]) == old_end
    assert prior["sla_hours"] == 2
    assert appeal.sla_hours_at_start == 8
    assert appeal.sla_anchor_at > old_end
    data, _, _ = await complete_stats.build_xlsx(session, "all")
    wb = load_workbook(BytesIO(data), read_only=True, data_only=True)
    rows = list(wb["Сроки"].values)
    assert len(rows) == 2
    assert rows[1][5] == ("да" if overdue else "нет")
    assert "на момент закрытия" in rows[1][6]


@pytest.mark.asyncio
async def test_missing_historical_calendar_stays_unknown(session):
    user = User(max_user_id=919992)
    session.add(user)
    await session.flush()
    appeal = await appeals.create_appeal(session, user, "Адрес", "Тема", "Текст", [])
    assert await appeals.close(session, appeal.id)
    appeal.sla_schedule_at_start = None
    await session.flush()
    assert await appeals.reopen(session, appeal.id) == "reopened"
    await session.refresh(appeal)
    assert appeal.sla_history[-1]["overdue"] is None


@pytest.mark.asyncio
async def test_unknown_delivery_time_is_not_treated_as_no_answer_at_close(session):
    user = User(max_user_id=919993)
    session.add(user)
    await session.flush()
    appeal = await appeals.create_appeal(session, user, "Адрес", "Тема", "Текст", [])
    start = datetime(2026, 10, 27, 21, tzinfo=UTC)
    appeal.created_at = appeal.sla_anchor_at = start
    assert appeal.sla_schedule_at_start is not None
    assert await appeals.close(session, appeal.id)
    appeal.closed_at = start + timedelta(hours=6)
    session.add(Message(appeal_id=appeal.id, direction="from_operator", text="Ответ",
                        response_kind="intermediate", created_at=start + timedelta(minutes=1),
                        delivery_source="manual_partial", delivery_uncertain_until=appeal.closed_at))
    await session.flush()
    assert await appeals.reopen(session, appeal.id) == "reopened"
    await session.refresh(appeal)
    prior = appeal.sla_history[-1]
    assert prior["first_response"] is None
    assert prior["overdue"] is None
    assert prior["assessment"] == "unknown_response_time"


@pytest.mark.asyncio
async def test_unreconciled_attempt_cannot_prove_absence_of_first_reply(session):
    user = User(max_user_id=919994)
    operator = Operator(max_user_id=919995, full_name="Оператор", role="aemr")
    session.add_all([user, operator])
    await session.flush()
    appeal = await appeals.create_appeal(session, user, "Адрес", "Тема", "Текст", [])
    appeal.created_at = appeal.sla_anchor_at = datetime.now(UTC) - timedelta(days=2)
    await session.commit()
    attempt_id = await operator_reply_attempts.reserve(
        appeal_id=appeal.id, operator_id=operator.id, source_key="pending-before-close",
        text="Ответ", attachments=[], is_final=True, part_plan=[[]])
    await operator_reply_attempts.note_part(attempt_id, 0, "sending", None)
    await operator_reply_attempts.mark_unknown(attempt_id)
    assert await appeals.close(session, appeal.id)
    assert await appeals.reopen(session, appeal.id) == "reopened"
    await session.refresh(appeal)
    assert appeal.sla_history[-1]["overdue"] is None
    assert appeal.sla_history[-1]["assessment"] == "unknown_response_time"


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_before_reply", [True, False])
async def test_pending_earlier_attempt_does_not_make_later_exact_reply_the_first(session, pending_before_reply):
    user = User(max_user_id=919996)
    operator = Operator(max_user_id=919997, full_name="Оператор", role="aemr")
    session.add_all([user, operator])
    await session.flush()
    appeal = await appeals.create_appeal(session, user, "Адрес", "Тема", "Текст", [])
    start = datetime(2026, 10, 27, 21, tzinfo=UTC)
    appeal.created_at = appeal.sla_anchor_at = start
    await session.commit()
    attempt_id = await operator_reply_attempts.reserve(
        appeal_id=appeal.id, operator_id=operator.id, source_key="pending-other-operator",
        text="Возможный ранний ответ", attachments=[], is_final=False, part_plan=[[]])
    await operator_reply_attempts.note_part(attempt_id, 0, "sending", None)
    await operator_reply_attempts.mark_unknown(attempt_id)
    attempt = await session.get(OperatorReplyAttempt, attempt_id)
    attempt.created_at = start + timedelta(hours=1 if pending_before_reply else 7)
    exact = start + timedelta(hours=6)
    session.add(Message(appeal_id=appeal.id, direction="from_operator", text="Точный ответ",
                        response_kind="intermediate", created_at=exact, delivery_source="confirmed"))
    await session.flush()
    assert await appeals.close(session, appeal.id)
    assert await appeals.reopen(session, appeal.id) == "reopened"
    await session.refresh(appeal)
    prior = appeal.sla_history[-1]
    if pending_before_reply:
        assert prior["overdue"] is None
        assert prior["first_response"] is None
        assert prior["assessment"] == "unknown_response_time"
    else:
        assert prior["overdue"] is True
        assert datetime.fromisoformat(prior["first_response"]) == exact
