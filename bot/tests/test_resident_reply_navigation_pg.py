"""Navigation never cancels delivery of an already submitted appeal.

Real PostgreSQL/services; the MAX transport is an in-memory fake only.
"""
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from aemr_bot.db.models import Appeal, DialogState, Message, Operator, OperatorReplyAttempt, User
from aemr_bot.handlers import menu, operator_reply
from aemr_bot.services import admin_card, appeals, users
from tests._helpers import make_event


@pytest.mark.asyncio
@pytest.mark.parametrize("is_final", [False, True])
@pytest.mark.parametrize("navigation", ["idle_menu", "pause_followup", "another_draft"])
async def test_reply_survives_resident_navigation(session, monkeypatch, navigation, is_final):
    consent = datetime(2026, 1, 1, tzinfo=UTC)
    resident = User(max_user_id=90701, first_name="Test resident", consent_pdn_at=consent,
                    subscribed_broadcast=False, dialog_state=DialogState.IDLE.value)
    operator = Operator(max_user_id=90702, full_name="Test operator", role="aemr")
    session.add_all([resident, operator])
    await session.flush()
    appeal = Appeal(user_id=resident.id, status="in_progress", summary="Submitted appeal",
                    attachments=[], topic="Test")
    session.add(appeal)
    await session.flush()
    if navigation != "idle_menu":
        state = (DialogState.AWAITING_FOLLOWUP_TEXT if navigation == "pause_followup"
                 else DialogState.AWAITING_SUMMARY)
        await users.set_state(session, resident.max_user_id, state, data={
            "draft_token": "unsubmitted-draft", "summary_chunks": ["Do not lose this"],
            "attachments": [], **({"appeal_id": appeal.id} if navigation == "pause_followup" else {}),
        })
    await session.commit()

    if navigation != "another_draft":
        # Exercise the real menu transition, including pause and privacy guard.
        resident_event = make_event(user_id=resident.max_user_id, send_returns_mid=True)
        await menu.open_main_menu(resident_event)
        assert resident_event.bot.send_message.await_count == 1
    await session.refresh(resident)
    expected_state = (DialogState.AWAITING_SUMMARY.value if navigation == "another_draft"
                      else DialogState.IDLE.value)
    assert resident.dialog_state == expected_state
    if navigation == "pause_followup":
        assert resident.appeal_draft["data"]["appeal_id"] == appeal.id
    saved_data, saved_draft = dict(resident.dialog_data), dict(resident.appeal_draft or {})
    await session.refresh(appeal)
    assert appeal.status == "in_progress"
    assert resident.consent_pdn_at == consent

    # No network client exists in this test. Only send_message is simulated;
    # delivery guards, reservation, per-part evidence and history stay real.
    event = make_event(user_id=operator.max_user_id, chat_id=123, send_returns_mid=True,
                       mid=f"operator-{navigation}-{is_final}")
    event.bot.send_message.return_value = SimpleNamespace(
        message=SimpleNamespace(body=SimpleNamespace(mid="fake-local-reply-mid")))
    render = AsyncMock()
    monkeypatch.setattr(admin_card, "render", render)
    monkeypatch.setattr(operator_reply, "_recent_replies", {})
    fresh = await appeals.get_by_id(session, appeal.id)
    assert await operator_reply._deliver_operator_reply(
        event, appeal=fresh, operator=operator, text="Local test response",
        audit_action="reply_via_command", is_final=is_final,
    )

    event.bot.send_message.assert_awaited_once()
    outgoing = event.bot.send_message.await_args.kwargs
    assert outgoing["user_id"] == resident.max_user_id
    assert "Local test response" in outgoing["text"]
    assert "chat_id" not in outgoing
    message = await session.scalar(select(Message).where(Message.appeal_id == appeal.id))
    assert message.text == "Local test response"
    assert message.direction == "from_operator"
    assert message.response_kind == ("final" if is_final else "intermediate")
    assert message.max_message_id == "fake-local-reply-mid"
    attempt = await session.scalar(select(OperatorReplyAttempt))
    assert attempt.state == "recorded"
    await session.refresh(appeal)
    assert appeal.status == ("answered" if is_final else "in_progress")
    await session.refresh(resident)
    assert resident.dialog_state == expected_state
    assert resident.dialog_data == saved_data and (resident.appeal_draft or {}) == saved_draft
    assert resident.consent_pdn_at == consent and resident.subscribed_broadcast is False
    render.assert_awaited_once()
    assert render.await_args.args[1].id == appeal.id
    assert render.await_args.kwargs["force_new"] is True
