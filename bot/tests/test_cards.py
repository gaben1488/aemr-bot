from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aemr_bot.services.cards import callback_message_id, send_or_edit_card


def _callback_event(*, mid: str = "m-old"):
    bot = AsyncMock()
    return SimpleNamespace(
        bot=bot,
        callback=SimpleNamespace(),
        message=SimpleNamespace(body=SimpleNamespace(mid=mid)),
        get_ids=lambda: {"chat_id": 42, "user_id": 777},
    )


def _message_event():
    bot = AsyncMock()
    return SimpleNamespace(
        bot=bot,
        callback=None,
        message=SimpleNamespace(body=SimpleNamespace(mid="incoming-user-message")),
        get_ids=lambda: {"chat_id": 42, "user_id": 777},
    )


class TestCallbackMessageId:
    def test_callback_has_mid(self) -> None:
        event = _callback_event(mid="abc")
        assert callback_message_id(event) == "abc"

    def test_plain_message_has_no_callback_mid(self) -> None:
        event = _message_event()
        assert callback_message_id(event) is None


class TestSendOrEditCard:
    @pytest.mark.asyncio
    async def test_callback_edits_pressed_card(self) -> None:
        event = _callback_event(mid="m-pressed")

        mid, edited = await send_or_edit_card(
            event,
            text="next screen",
            attachments=["keyboard"],
        )

        event.bot.edit_message.assert_called_once()
        kwargs = event.bot.edit_message.call_args.kwargs
        assert kwargs["message_id"] == "m-pressed"
        assert kwargs["text"] == "next screen"
        assert kwargs["attachments"] == ["keyboard"]
        event.bot.send_message.assert_not_called()
        assert mid == "m-pressed"
        assert edited is True

    @pytest.mark.asyncio
    async def test_plain_message_sends_new_card(self) -> None:
        event = _message_event()
        event.bot.send_message.return_value = SimpleNamespace(
            message=SimpleNamespace(body=SimpleNamespace(mid="m-new"))
        )

        mid, edited = await send_or_edit_card(
            event,
            text="next screen",
            attachments=[],
        )

        event.bot.edit_message.assert_not_called()
        event.bot.send_message.assert_called_once()
        kwargs = event.bot.send_message.call_args.kwargs
        assert kwargs["chat_id"] == 42
        assert kwargs["text"] == "next screen"
        assert mid == "m-new"
        assert edited is False

    @pytest.mark.asyncio
    async def test_force_new_message_skips_edit_even_for_callback(self) -> None:
        event = _callback_event(mid="m-pressed")
        event.bot.send_message.return_value = SimpleNamespace(
            message=SimpleNamespace(body=SimpleNamespace(mid="m-forced"))
        )

        mid, edited = await send_or_edit_card(
            event,
            text="screen after visible user input",
            attachments=[],
            force_new_message=True,
        )

        event.bot.edit_message.assert_not_called()
        event.bot.send_message.assert_called_once()
        assert mid == "m-forced"
        assert edited is False

    @pytest.mark.asyncio
    async def test_edit_failure_falls_back_to_send(self) -> None:
        event = _callback_event(mid="m-stale")
        event.bot.edit_message.side_effect = RuntimeError("message is gone")
        event.bot.send_message.return_value = SimpleNamespace(
            message=SimpleNamespace(body=SimpleNamespace(mid="m-fallback"))
        )

        mid, edited = await send_or_edit_card(
            event,
            text="fallback screen",
            attachments=[],
        )

        event.bot.edit_message.assert_called_once()
        event.bot.send_message.assert_called_once()
        assert mid == "m-fallback"
        assert edited is False
