"""Unrecorded MAX attempts are uncertainty, not evidence of no first reply."""
from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from openpyxl import load_workbook

from aemr_bot.db.models import Message, Operator, OperatorReplyAttempt, User
from aemr_bot.services import appeals, complete_stats, queue_panel
from aemr_bot.services import operator_reply_attempts as attempts


async def pending_case(session, state="unknown"):
    user = User(max_user_id=922001)
    operator = Operator(max_user_id=922002, full_name="Оператор", role="aemr")
    session.add_all([user, operator])
    await session.flush()
    appeal = await appeals.create_appeal(session, user, "Адрес", "Тема", "Текст", [])
    appeal.created_at = appeal.sla_anchor_at = datetime.now(UTC) - timedelta(days=3)
    await session.commit()
    attempt_id = await attempts.reserve(appeal_id=appeal.id, operator_id=operator.id,
        source_key="pending-evidence", text="Ответ", attachments=[], is_final=False, part_plan=[[]])
    await attempts.note_part(attempt_id, 0, "sending", None)
    if state == "unknown":
        await attempts.mark_unknown(attempt_id)
    elif state == "confirmed":
        await attempts.note_part(attempt_id, 0, "confirmed", "known-max-id")
    return appeal, operator, attempt_id


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["sending", "unknown", "confirmed"])
async def test_unrecorded_attempt_is_visible_in_queue_and_export_not_overdue_alert(session, state):
    appeal, _, _ = await pending_case(session, state)
    assert await appeals.first_operator_reply_times(session, [appeal.id]) == {appeal.id: None}
    assert await appeals.latest_intermediate_reply_times(session, [appeal.id]) == {appeal.id: None}
    assert appeal not in await appeals.find_overdue_unanswered(session, 1)
    text, _ = await queue_panel._snapshot()
    assert "Время ответа требует сверки" in text
    assert "Просрочен первый ответ" not in text
    data, _, _ = await complete_stats.build_xlsx(session, "all")
    wb = load_workbook(BytesIO(data), read_only=True, data_only=True)
    summary = {row[0]: row[1] for row in list(wb["Сводка"].values)[1:]}
    assert summary["Время или период первого ответа неизвестен"] == 1
    assert summary["Обращений с первым ответом в текущем периоде"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("earlier", [True, False])
async def test_order_of_pending_attempt_and_exact_reply_is_preserved(session, earlier):
    appeal, _, attempt_id = await pending_case(session)
    exact = datetime.now(UTC) - timedelta(hours=1)
    pending = await session.get(OperatorReplyAttempt, attempt_id)
    pending.created_at = exact + timedelta(minutes=-1 if earlier else 1)
    session.add(Message(appeal_id=appeal.id, direction="from_operator", text="Точный ответ",
                        response_kind="intermediate", created_at=exact, delivery_source="confirmed"))
    await session.commit()
    assert await appeals.first_operator_reply_times(session, [appeal.id]) == {
        appeal.id: None if earlier else exact}
    # Any unresolved attempt can still affect the subsequent-work timer.
    assert await appeals.latest_intermediate_reply_times(session, [appeal.id]) == {appeal.id: None}


@pytest.mark.asyncio
async def test_not_delivered_resolution_restores_unanswered_status(session):
    appeal, operator, attempt_id = await pending_case(session)
    assert await attempts.resolve_unknown(session, attempt_id, delivered=False, actor_max_id=operator.max_user_id)
    await session.commit()
    assert await appeals.first_operator_reply_times(session, [appeal.id]) == {}
    assert await appeals.latest_intermediate_reply_times(session, [appeal.id]) == {}
    assert appeal in await appeals.find_overdue_unanswered(session, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("has_current_reply", [False, True])
async def test_fully_confirmed_old_attempt_does_not_pollute_new_period(session, has_current_reply):
    appeal, _, attempt_id = await pending_case(session, "confirmed")
    now = datetime.now(UTC)
    appeal.sla_anchor_at = now - timedelta(days=2)
    pending = await session.get(OperatorReplyAttempt, attempt_id)
    pending.created_at = now - timedelta(days=3)
    pending.confirmed_at = pending.last_activity_at = now - timedelta(days=3, minutes=-1)
    exact = now - timedelta(days=1)
    if has_current_reply:
        session.add(Message(appeal_id=appeal.id, direction="from_operator", text="Новый ответ",
            response_kind="intermediate", created_at=exact, delivery_source="confirmed"))
    await session.commit()
    expected = {appeal.id: exact} if has_current_reply else {}
    assert await appeals.first_operator_reply_times(session, [appeal.id]) == expected
    assert await appeals.latest_intermediate_reply_times(session, [appeal.id]) == expected
    assert (appeal in await appeals.find_overdue_unanswered(session, 1)) is not has_current_reply


@pytest.mark.asyncio
@pytest.mark.parametrize("reader", [appeals.first_operator_reply_times,
                                    appeals.latest_intermediate_reply_times])
async def test_reconciliation_between_reads_cannot_erase_reply_evidence(session, reader):
    from aemr_bot.db.session import session_scope

    appeal, _, attempt_id = await pending_case(session, "confirmed")

    class ReconcileAfterSnapshot:
        reconciled = False

        async def stream(self, query):
            rows = await session.stream(query)
            if not self.reconciled:
                self.reconciled = True
                async with session_scope() as repair:
                    assert await attempts.record_confirmed(repair, attempt_id)
            return rows

    observed = await reader(ReconcileAfterSnapshot(), [appeal.id])
    # A single snapshot may show pending or exact evidence, never no reply.
    assert appeal.id in observed


@pytest.mark.asyncio
async def test_multipart_confirmation_crossing_anchor_remains_current_evidence(session):
    appeal, _, attempt_id = await pending_case(session, "confirmed")
    now = datetime.now(UTC)
    appeal.sla_anchor_at = now - timedelta(days=2)
    pending = await session.get(OperatorReplyAttempt, attempt_id)
    pending.created_at = now - timedelta(days=3)
    # confirmed_at is the FIRST part, not the completion bound of all parts.
    pending.confirmed_at = now - timedelta(days=3, minutes=-1)
    pending.last_activity_at = now - timedelta(days=1)
    await session.commit()
    assert await appeals.first_operator_reply_times(session, [appeal.id]) == {appeal.id: None}
    assert await appeals.latest_intermediate_reply_times(session, [appeal.id]) == {appeal.id: None}
    assert appeal not in await appeals.find_overdue_unanswered(session, 1)
