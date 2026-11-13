"""A resident event must not enumerate another resident's admin cards."""
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from aemr_bot.db.models import Appeal, User
from aemr_bot.handlers import menu
from aemr_bot.services import admin_card, admin_events, appeals
from tests._helpers import make_event


@pytest.mark.asyncio
async def test_resident_notice_lists_only_own_open_non_erased_appeals(session, monkeypatch):
    now = datetime.now(UTC)
    resident = User(max_user_id=90711, first_name="Resident", consent_pdn_at=now)
    other = User(max_user_id=90712, first_name="Other resident", consent_pdn_at=now)
    session.add_all([resident, other])
    await session.flush()
    # More than the old global LIMIT: filtering that global list in Python
    # would incorrectly hide this resident's newer appeals.
    session.add_all([Appeal(user_id=other.id, status="new", summary="Other",
                           created_at=now - timedelta(days=1)) for _ in range(501)])
    own = [Appeal(user_id=resident.id, status=status, summary="Mine", created_at=now)
           for status in ("new", "in_progress", "answered", "closed")]
    erased = Appeal(user_id=resident.id, status="new", content_erased_at=now)
    session.add_all([*own, erased])
    await session.commit()
    expected = {appeal.id for appeal in own[:2]}
    actual = await appeals.list_unanswered_for_user(session, resident.id)
    assert {appeal.id for appeal in actual} == expected

    # Keep the handler and its query real; inspect exactly the identifiers
    # handed to the administrator notice, without any MAX connection.
    notify, render = AsyncMock(), AsyncMock()
    monkeypatch.setattr(admin_events, "notify_consent_revoked", notify)
    monkeypatch.setattr(admin_card, "render", render)
    monkeypatch.setattr(menu, "_send_or_edit_menu", AsyncMock())
    event = make_event(user_id=resident.max_user_id)
    await menu.do_consent_revoke(event, resident.max_user_id)
    notify.assert_awaited_once()
    assert notify.await_args.kwargs["max_user_id"] == resident.max_user_id
    assert set(notify.await_args.kwargs["open_appeal_ids"]) == expected
    # Current UX reports open IDs once, without reposting historical cards.
    render.assert_not_awaited()
    event.bot.send_message.assert_not_awaited()
