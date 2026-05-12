"""Финальные P1-регрессии по audit-cycle.

Здесь только сценарии, где был найден конкретный риск:
- нельзя переоткрывать обращения, закрытые из-за отзыва/удаления/блокировки;
- retention должен стирать не только text, но и attachments даже при уже пустом text;
- IT-список подписчиков должен совпадать с фактической eligibility рассылки;
- IT-список consented не должен показывать удалённых/заблокированных как активных.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from aemr_bot.db.models import Appeal, AppealStatus, Message, User
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import users as users_service


@pytest.mark.asyncio
async def test_reopen_refuses_closed_due_to_revoke(session) -> None:
    user = await users_service.get_or_create(session, max_user_id=101, first_name="A")
    appeal = await appeals_service.create_appeal(
        session,
        user=user,
        address="ул. A",
        topic="T",
        summary="S",
        attachments=[],
    )
    await appeals_service.close(session, appeal.id)
    await session.execute(
        update(Appeal)
        .where(Appeal.id == appeal.id)
        .values(closed_due_to_revoke=True)
    )
    await session.flush()

    assert await appeals_service.reopen(session, appeal.id) is False
    refreshed = await appeals_service.get_by_id(session, appeal.id)
    assert refreshed is not None
    assert refreshed.status == AppealStatus.CLOSED.value
    assert refreshed.closed_due_to_revoke is True


@pytest.mark.asyncio
async def test_purge_old_appeals_content_redacts_attachments_even_without_text(session) -> None:
    user = await users_service.get_or_create(session, max_user_id=102, first_name="A")
    appeal = await appeals_service.create_appeal(
        session,
        user=user,
        address="ул. A",
        topic="T",
        summary="S",
        attachments=[{"type": "image", "token": "old-appeal-media"}],
    )
    msg = await appeals_service.add_user_message(
        session,
        appeal=appeal,
        text="старый текст",
        attachments=[{"type": "file", "token": "old-message-media"}],
    )
    old_closed_at = datetime.now(timezone.utc) - timedelta(days=366 * 6)
    await session.execute(
        update(Appeal)
        .where(Appeal.id == appeal.id)
        .values(
            status=AppealStatus.CLOSED.value,
            closed_at=old_closed_at,
            summary=None,
        )
    )
    await session.execute(
        update(Message)
        .where(Message.id == msg.id)
        .values(text=None)
    )
    await session.flush()

    purged_appeals, purged_messages = await appeals_service.purge_old_appeals_content(
        session,
        years=5,
    )

    assert purged_appeals == 1
    assert purged_messages == 1
    stored_appeal = await session.scalar(select(Appeal).where(Appeal.id == appeal.id))
    stored_message = await session.scalar(select(Message).where(Message.id == msg.id))
    assert stored_appeal is not None
    assert stored_message is not None
    assert stored_appeal.summary is None
    assert stored_appeal.attachments == []
    assert stored_message.text is None
    assert stored_message.attachments == []


@pytest.mark.asyncio
async def test_list_subscribers_matches_broadcast_eligibility(session) -> None:
    eligible = await users_service.get_or_create(session, max_user_id=201, first_name="A")
    legacy_no_broadcast_consent = await users_service.get_or_create(
        session, max_user_id=202, first_name="B"
    )
    deleted = await users_service.get_or_create(session, max_user_id=203, first_name="Удалено")
    blocked = await users_service.get_or_create(session, max_user_id=204, first_name="D")
    now = datetime.now(timezone.utc)
    await session.execute(
        update(User)
        .where(User.id == eligible.id)
        .values(subscribed_broadcast=True, consent_broadcast_at=now)
    )
    await session.execute(
        update(User)
        .where(User.id == legacy_no_broadcast_consent.id)
        .values(subscribed_broadcast=True, consent_broadcast_at=None)
    )
    await session.execute(
        update(User)
        .where(User.id == deleted.id)
        .values(subscribed_broadcast=True, consent_broadcast_at=now)
    )
    await session.execute(
        update(User)
        .where(User.id == blocked.id)
        .values(subscribed_broadcast=True, consent_broadcast_at=now, is_blocked=True)
    )
    await session.flush()

    rows = await users_service.list_subscribers(session, limit=10)
    ids = {u.max_user_id for u in rows}
    assert ids == {201}


@pytest.mark.asyncio
async def test_list_consented_excludes_blocked_and_deleted(session) -> None:
    active = await users_service.get_or_create(session, max_user_id=301, first_name="A")
    deleted = await users_service.get_or_create(session, max_user_id=302, first_name="Удалено")
    blocked = await users_service.get_or_create(session, max_user_id=303, first_name="C")
    now = datetime.now(timezone.utc)
    await session.execute(
        update(User)
        .where(User.id.in_([active.id, deleted.id, blocked.id]))
        .values(consent_pdn_at=now)
    )
    await session.execute(
        update(User).where(User.id == blocked.id).values(is_blocked=True)
    )
    await session.flush()

    rows = await users_service.list_consented(session, limit=10)
    ids = {u.max_user_id for u in rows}
    assert 301 in ids
    assert 302 not in ids
    assert 303 not in ids
