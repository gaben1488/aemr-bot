"""The existing submit screen should let residents check the selected case."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aemr_bot.db.models import DialogState
from aemr_bot.handlers import appeal_funnel
from tests._helpers import fake_session_scope, make_event


@pytest.mark.asyncio
@pytest.mark.parametrize("followup", [False, True])
async def test_preview_shows_case_context_without_an_extra_step(monkeypatch, followup):
    user = SimpleNamespace(
        dialog_state=(DialogState.AWAITING_FOLLOWUP_TEXT if followup else DialogState.AWAITING_SUMMARY).value,
        dialog_data={"draft_token": "draft", "appeal_id": 71,
                     "locality": "Елизово", "address": "Ленина, 10", "topic": "Дороги",
                     "summary_chunks": ["Яма во дворе"], "attachments": []},
    )

    @asynccontextmanager
    async def current_user(_):
        yield None, user

    monkeypatch.setattr(appeal_funnel, "current_user", current_user)
    screen = AsyncMock()
    monkeypatch.setattr(appeal_funnel, "send_or_edit_screen", screen)
    await appeal_funnel.show_summary_draft(make_event(), 42)
    assert screen.await_count == 1
    text = screen.await_args.kwargs["text"]
    assert "Яма во дворе" in text
    if followup:
        assert "дополнения к № 71" in text
    else:
        for value in ("Елизово", "Ленина, 10", "Дороги"):
            assert value in text


@pytest.mark.asyncio
async def test_empty_topics_does_not_claim_unperformed_notification(monkeypatch):
    monkeypatch.setattr(appeal_funnel, "session_scope", fake_session_scope)
    monkeypatch.setattr(appeal_funnel.settings_store, "get", AsyncMock(return_value=[]))
    monkeypatch.setattr(appeal_funnel.users_service, "reset_state", AsyncMock())
    event = make_event()
    await appeal_funnel.on_awaiting_topic(event, None, "текст", 42)
    text = event.message.answer.await_args.args[0]
    assert "сообщили" not in text
    assert "Попробуйте позже" in text
    assert event.message.answer.await_args.kwargs["attachments"]
