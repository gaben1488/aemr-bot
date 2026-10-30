"""Opening an appeal is navigation, not a new historical event."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_open_uses_navigation_screen_and_neutralizes_reply_markers(monkeypatch):
    from aemr_bot.handlers import admin_callback_dispatch as dispatch

    appeal = SimpleNamespace(
        id=107, status="new", user=SimpleNamespace(is_blocked=False),
        closed_due_to_revoke=False,
    )

    @asynccontextmanager
    async def scope():
        yield object()

    monkeypatch.setattr(dispatch, "session_scope", scope)
    monkeypatch.setattr(dispatch, "get_operator", AsyncMock(return_value=SimpleNamespace(id=7, role="it")))
    monkeypatch.setattr(dispatch, "ack_callback", AsyncMock())
    monkeypatch.setattr(dispatch.appeals_service, "get_by_id_with_messages", AsyncMock(return_value=appeal))
    monkeypatch.setattr(dispatch.admin_relay, "collect_appeal_attachments", lambda _: [])
    monkeypatch.setattr(dispatch.card_format, "admin_card", lambda *_: "Текст 🆔 №999\n🆔 №107")
    screen = AsyncMock()
    monkeypatch.setattr(dispatch, "send_or_edit_screen", screen)
    render = AsyncMock()
    monkeypatch.setattr("aemr_bot.services.admin_card.render", render)
    await dispatch._op_open_card(SimpleNamespace(bot=object()), 107)
    render.assert_not_awaited()
    screen.assert_awaited_once()
    assert "🆔 №" not in screen.await_args.kwargs["text"]
    assert "кнопк" in screen.await_args.kwargs["text"].lower()


@pytest.mark.asyncio
async def test_open_edits_fresh_list_but_preserves_historical_event(monkeypatch):
    from aemr_bot.handlers import admin_callback_dispatch as dispatch
    from aemr_bot.utils import menu_tracker
    from tests.test_freshness_full_coverage import (
        ADMIN_CHAT_ID,
        _make_appeal,
        _make_bot,
        _make_event,
    )

    appeal = _make_appeal(appeal_id=107)

    @asynccontextmanager
    async def scope():
        yield object()

    monkeypatch.setattr(dispatch, "session_scope", scope)
    monkeypatch.setattr(dispatch.cfg, "admin_group_id", ADMIN_CHAT_ID)
    monkeypatch.setattr(dispatch, "get_operator", AsyncMock(return_value=SimpleNamespace(id=7, role="it")))
    monkeypatch.setattr(dispatch, "ack_callback", AsyncMock())
    monkeypatch.setattr(dispatch.appeals_service, "get_by_id_with_messages", AsyncMock(return_value=appeal))
    bot = _make_bot(send_mids=["view-after-event"])
    menu_tracker.clear_all()
    try:
        menu_tracker.note_editable_send(ADMIN_CHAT_ID, "current-list")
        await dispatch._op_open_card(_make_event(bot=bot, callback_mid="current-list"), 107)
        bot.edit_message.assert_awaited_once()
        bot.send_message.assert_not_awaited()
        assert bot.edit_message.await_args.kwargs["message_id"] == "current-list"
        assert bot.edit_message.await_args.kwargs["notify"] is False
        assert "потяните карточку" not in bot.edit_message.await_args.kwargs["text"]

        # A later historical event cannot be overwritten by an old list button.
        menu_tracker.note_event(ADMIN_CHAT_ID, "historical-event")
        await dispatch._op_open_card(_make_event(bot=bot, callback_mid="current-list"), 107)
        assert bot.edit_message.await_count == 1
        bot.send_message.assert_awaited_once()
        assert menu_tracker.get_last_menu_mid(ADMIN_CHAT_ID) == "view-after-event"
    finally:
        menu_tracker.clear_all()
