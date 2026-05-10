"""Тесты на utils/event — извлечение полей из MAX events.

MAX-events могут приходить в нескольких формах: Update, MessageCreated,
MessageCallback, голый Message. Хелперы должны работать для всех."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from aemr_bot.utils.event import (
    extract_message_id,
    get_chat_id,
    get_message_text,
    get_payload,
    get_user_id,
)


class TestGetUserId:
    def test_message_created_with_sender(self) -> None:
        event = SimpleNamespace(
            message=SimpleNamespace(
                sender=SimpleNamespace(user_id=42),
                recipient=None,
            ),
        )
        assert get_user_id(event) == 42

    def test_callback_user_field(self) -> None:
        event = SimpleNamespace(
            callback=SimpleNamespace(user=SimpleNamespace(user_id=99)),
        )
        assert get_user_id(event) == 99

    def test_empty_event(self) -> None:
        assert get_user_id(SimpleNamespace()) is None


class TestGetChatId:
    def test_message_recipient(self) -> None:
        event = SimpleNamespace(
            message=SimpleNamespace(
                sender=None,
                recipient=SimpleNamespace(chat_id=12345),
            ),
        )
        assert get_chat_id(event) == 12345

    def test_no_chat(self) -> None:
        assert get_chat_id(SimpleNamespace()) is None


class TestGetPayload:
    def test_callback_payload(self) -> None:
        event = SimpleNamespace(
            callback=SimpleNamespace(payload="menu:new_appeal"),
        )
        assert get_payload(event) == "menu:new_appeal"

    def test_no_callback(self) -> None:
        assert get_payload(SimpleNamespace()) == ""


class TestGetMessageText:
    def test_message_with_body_text(self) -> None:
        event = SimpleNamespace(
            message=SimpleNamespace(
                body=SimpleNamespace(text="привет"),
            ),
        )
        assert get_message_text(event) == "привет"

    def test_no_text_returns_empty(self) -> None:
        assert get_message_text(SimpleNamespace()) == ""

    def test_none_text(self) -> None:
        event = SimpleNamespace(
            message=SimpleNamespace(body=SimpleNamespace(text=None)),
        )
        assert get_message_text(event) == ""


class TestExtractMessageId:
    def test_sended_message_form(self) -> None:
        """SendedMessage из bot.send_message: .message.body.mid"""
        sent = SimpleNamespace(
            message=SimpleNamespace(body=SimpleNamespace(mid="mid-abc")),
        )
        assert extract_message_id(sent) == "mid-abc"

    def test_bare_message_form(self) -> None:
        """Голый Message: .body.mid"""
        sent = SimpleNamespace(body=SimpleNamespace(mid="mid-xyz"))
        assert extract_message_id(sent) == "mid-xyz"

    def test_none(self) -> None:
        assert extract_message_id(None) is None

    def test_empty_object(self) -> None:
        assert extract_message_id(SimpleNamespace()) is None


@pytest.mark.asyncio
async def test_ack_callback_swallows_exceptions() -> None:
    """ack_callback не должен пропустить exception от bot.answer_on_callback —
    иначе любая ошибка MAX-API сломает обработку callback'а."""
    from aemr_bot.utils.event import ack_callback

    class _FailingBot:
        async def answer_on_callback(self, **_: object) -> None:
            raise RuntimeError("network error")

    event = SimpleNamespace(
        bot=_FailingBot(),
        callback=SimpleNamespace(callback_id="cb-123"),
    )
    # Должно проглотить exception
    await ack_callback(event)
