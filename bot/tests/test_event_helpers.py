"""Тесты на utils/event — извлечение полей из MAX events.

MAX-events могут приходить в нескольких формах: Update, MessageCreated,
MessageCallback, голый Message. Хелперы должны работать для всех."""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aemr_bot.utils.event import (
    extract_message_id,
    get_callback_message_id,
    get_chat_id,
    get_message_text,
    get_payload,
    get_user_id,
    send_or_edit_screen,
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


class TestCallbackMessageId:
    def test_callback_message_mid(self) -> None:
        event = SimpleNamespace(
            callback=SimpleNamespace(payload="menu:settings"),
            message=SimpleNamespace(body=SimpleNamespace(mid="m-current")),
        )
        assert get_callback_message_id(event) == "m-current"

    def test_plain_message_has_no_callback_mid(self) -> None:
        event = SimpleNamespace(
            message=SimpleNamespace(body=SimpleNamespace(mid="m-current")),
        )
        assert get_callback_message_id(event) is None


class TestSendOrEditScreen:
    def setup_method(self) -> None:
        # Изоляция menu_tracker между тестами: in-memory dict
        # переживает несколько тестов в одном процессе.
        from aemr_bot.utils import menu_tracker
        menu_tracker.clear_all()

    @pytest.mark.asyncio
    async def test_callback_edits_current_card(self) -> None:
        """Callback от АКТУАЛЬНОЙ карточки-меню (mid в tracker'е) → edit.
        Сценарий: оператор кликает кнопку на свежем меню, бот редактирует."""
        from aemr_bot.utils import menu_tracker
        bot = SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock())
        event = SimpleNamespace(
            bot=bot,
            callback=SimpleNamespace(payload="menu:settings"),
            message=SimpleNamespace(
                sender=SimpleNamespace(user_id=42),
                recipient=SimpleNamespace(chat_id=100),
                body=SimpleNamespace(mid="m-current"),
            ),
        )
        # «m-current» — последняя отправленная карточка-меню в этом чате
        menu_tracker.set_last_menu_mid(100, "m-current")

        await send_or_edit_screen(event, text="Экран", attachments=["kb"])

        bot.edit_message.assert_called_once_with(
            message_id="m-current",
            text="Экран",
            attachments=["kb"],
        )
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_force_new_message_sends_below_visible_user_input(self) -> None:
        bot = SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock())
        event = SimpleNamespace(
            bot=bot,
            callback=SimpleNamespace(payload="topic:1"),
            message=SimpleNamespace(
                sender=SimpleNamespace(user_id=42),
                recipient=SimpleNamespace(chat_id=100),
                body=SimpleNamespace(mid="m-current"),
            ),
        )

        await send_or_edit_screen(
            event,
            text="Следующий шаг",
            attachments=["kb"],
            force_new_message=True,
        )

        bot.edit_message.assert_not_called()
        bot.send_message.assert_called_once_with(
            chat_id=100,
            user_id=None,
            text="Следующий шаг",
            attachments=["kb"],
        )

    @pytest.mark.asyncio
    async def test_edit_failure_falls_back_to_send(self) -> None:
        """Существующее поведение: если edit_message бросил исключение
        (например, MAX отверг по слишком старому mid), send_or_edit_screen
        всё равно шлёт новое — оператор увидит ответ. Тут tracker
        предзаполнен «правильным» mid, edit пытается выполниться, падает,
        fallback срабатывает."""
        from aemr_bot.utils import menu_tracker
        bot = SimpleNamespace(
            edit_message=AsyncMock(side_effect=RuntimeError("MAX edit failed")),
            send_message=AsyncMock(),
        )
        event = SimpleNamespace(
            bot=bot,
            callback=SimpleNamespace(payload="menu:settings"),
            message=SimpleNamespace(
                sender=SimpleNamespace(user_id=42),
                recipient=SimpleNamespace(chat_id=100),
                body=SimpleNamespace(mid="m-current"),
            ),
        )
        menu_tracker.set_last_menu_mid(100, "m-current")

        await send_or_edit_screen(event, text="Экран", attachments=["kb"])

        bot.edit_message.assert_called_once()
        bot.send_message.assert_called_once_with(
            chat_id=100,
            user_id=None,
            text="Экран",
            attachments=["kb"],
        )

    @pytest.mark.asyncio
    async def test_plain_direct_event_without_chat_sends_by_user_id(self) -> None:
        bot = SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock())
        event = SimpleNamespace(
            bot=bot,
            user=SimpleNamespace(user_id=42),
        )

        await send_or_edit_screen(event, text="Личное сообщение")

        bot.edit_message.assert_not_called()
        bot.send_message.assert_called_once_with(
            chat_id=None,
            user_id=42,
            text="Личное сообщение",
            attachments=[],
        )


@pytest.mark.asyncio
async def test_ack_callback_swallows_exceptions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ack_callback не должен пропустить exception от event.ack —
    иначе любая ошибка MAX-API сломает обработку callback'а."""
    from aemr_bot.utils.event import ack_callback

    ack = AsyncMock(side_effect=RuntimeError("network error"))
    event = SimpleNamespace(ack=ack)
    with caplog.at_level(logging.DEBUG, logger="aemr_bot.utils.event"):
        result = await ack_callback(event, "готово")

    # Дошли до реального event.ack (а не вышли раньше по callable-guard),
    # исключение проглочено и не пробросилось наружу, в debug-лог ушла
    # запись о неудаче.
    assert result is None
    ack.assert_awaited_once_with(notification="готово")
    assert any(
        "не удалось подтвердить callback" in rec.message
        for rec in caplog.records
    )
