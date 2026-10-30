from datetime import UTC, datetime, timedelta

import pytest


@pytest.mark.asyncio
async def test_first_reply_removes_appeal_from_unanswered_sla_but_keeps_it_open(session, monkeypatch):
    from aemr_bot.db.models import Appeal, Message, User
    from aemr_bot.services import appeals, sla

    user = User(max_user_id=514201, first_name="Иван")
    session.add(user)
    await session.flush()
    appeal = Appeal(user_id=user.id, status="in_progress", created_at=datetime.now(UTC) - timedelta(days=2),
                    summary="Сообщение", attachments=[])
    session.add(appeal)
    await session.flush()
    session.add(Message(appeal_id=appeal.id, direction="from_operator", text="Промежуточный ответ"))
    await session.flush()
    monkeypatch.setattr(sla, "is_overdue", lambda *args: True)
    assert appeal in await appeals.list_unanswered(session)
    assert appeal not in await appeals.find_overdue_unanswered(session, 4)


@pytest.mark.asyncio
async def test_reopen_keeps_prior_period_and_starts_new_first_reply_clock(session, monkeypatch):
    from aemr_bot.db.models import Appeal, Message, User
    from aemr_bot.services import appeals, work_schedule

    user = User(max_user_id=514202, first_name="Иван")
    session.add(user)
    await session.flush()
    start = datetime(2026, 6, 16, 0, tzinfo=UTC)
    appeal = Appeal(user_id=user.id, status="answered", created_at=start, sla_hours_at_start=4,
                    sla_schedule_at_start=work_schedule.environment(),
                    answered_at=start + timedelta(days=1), summary="Сообщение", attachments=[])
    session.add(appeal)
    await session.flush()
    session.add(Message(appeal_id=appeal.id, direction="from_operator", text="Ответ",
                        created_at=start + timedelta(days=1)))
    await session.flush()
    assert await appeals.reopen(session, appeal.id) == "reopened"
    await session.refresh(appeal)
    assert appeal.sla_anchor_at > start
    assert appeal.sla_history[0]["overdue"] is True
    assert not await appeals.first_operator_reply_times(session, [appeal.id])


@pytest.mark.asyncio
async def test_reopen_does_not_invent_old_sla_when_setting_was_not_saved(session):
    from aemr_bot.db.models import Appeal, Message, User
    from aemr_bot.services import appeals

    user = User(max_user_id=514203, first_name="Анна")
    session.add(user)
    await session.flush()
    start = datetime.now(UTC) - timedelta(days=2)
    appeal = Appeal(user_id=user.id, status="answered", created_at=start,
                    answered_at=start + timedelta(days=1), summary="Сообщение", attachments=[])
    session.add(appeal)
    await session.flush()
    session.add(Message(appeal_id=appeal.id, direction="from_operator", text="Ответ",
                        created_at=start + timedelta(days=1)))
    await session.flush()
    assert await appeals.reopen(session, appeal.id) == "reopened"
    await session.refresh(appeal)
    assert appeal.sla_history[0]["sla_hours"] is None
    assert appeal.sla_history[0]["overdue"] is None
    assert appeal.sla_history[0]["assessment"] == "at_reopen_legacy"


@pytest.mark.asyncio
async def test_latest_intermediate_response_ignores_old_period_and_newer_other_reply(session):
    from aemr_bot.db.models import Appeal, Message, User
    from aemr_bot.services import appeals

    user = User(max_user_id=514204, first_name="Анна")
    session.add(user)
    await session.flush()
    start = datetime.now(UTC) - timedelta(days=2)
    first = Appeal(user_id=user.id, status="in_progress", created_at=start, sla_anchor_at=start,
                   summary="А", attachments=[])
    second = Appeal(user_id=user.id, status="in_progress", created_at=start, sla_anchor_at=start,
                    summary="Б", attachments=[])
    session.add_all([first, second])
    await session.flush()
    session.add_all([
        Message(appeal_id=first.id, direction="from_operator", response_kind="intermediate", text="Ответ",
                created_at=start + timedelta(hours=1)),
        Message(appeal_id=first.id, direction="from_user", text="Дополнение", created_at=start + timedelta(hours=2)),
        Message(appeal_id=second.id, direction="from_operator", response_kind="intermediate", text="Ответ",
                created_at=start + timedelta(hours=1)),
        Message(appeal_id=second.id, direction="from_operator", response_kind=None, text="Старый тип",
                created_at=start + timedelta(hours=2)),
    ])
    await session.flush()
    result = await appeals.latest_intermediate_reply_times(session, [first.id, second.id])
    assert first.id in result
    assert second.id not in result
