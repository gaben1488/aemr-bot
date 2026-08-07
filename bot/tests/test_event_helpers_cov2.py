"""Покрытие непокрытых веток utils/event.

Базовый test_event_helpers.py покрывает get_ids/get_payload/
get_message_text/extract_message_id(основные)/send_or_edit_screen, но
оставляет без тестов:
- get_first_name: три источника (user / callback.user / message.sender)
  и None.
- get_message_link / get_message_body: None при отсутствии message.
- extract_message_id: унаследованный путь через .message_id и
  fall-through, когда .message.body есть, но mid=None.
- get_ids: fallback-ветку, когда event.get_ids() кидает исключение.
- get_chat_id fallback через event.chat_id.
- send / send_to / reply: happy-path и ранний выход при bot=None.
- send_or_edit_screen: ранний выход при bot=None.

Все стабы — SimpleNamespace + AsyncMock bot, без БД.
"""
from __future__ import annotations

from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from aemr_bot.utils import event as ev


class TestGetFirstName:
    def test_from_user_field(self) -> None:
        assert ev.get_first_name(NS(user=NS(first_name="Иван"))) == "Иван"

    def test_from_callback_user(self) -> None:
        e = NS(user=None, callback=NS(user=NS(first_name="Пётр")))
        assert ev.get_first_name(e) == "Пётр"

    def test_from_message_sender(self) -> None:
        e = NS(
            user=None,
            callback=None,
            message=NS(sender=NS(first_name="Анна")),
        )
        assert ev.get_first_name(e) == "Анна"

    def test_user_present_but_empty_name_falls_through(self) -> None:
        # user есть, но first_name пустой → идём к callback.user.
        e = NS(
            user=NS(first_name=""),
            callback=NS(user=NS(first_name="Лев")),
        )
        assert ev.get_first_name(e) == "Лев"

    def test_none_when_nothing(self) -> None:
        assert ev.get_first_name(NS()) is None

    def test_callback_without_user_falls_to_message(self) -> None:
        e = NS(user=None, callback=NS(user=None), message=NS(sender=NS(first_name="Зоя")))
        assert ev.get_first_name(e) == "Зоя"


class TestMessageLinkAndBody:
    def test_link_present(self) -> None:
        e = NS(message=NS(link="https://max/msg/1"))
        assert ev.get_message_link(e) == "https://max/msg/1"

    def test_link_none_when_no_message(self) -> None:
        assert ev.get_message_link(NS()) is None

    def test_body_present(self) -> None:
        body = NS(text="t")
        assert ev.get_message_body(NS(message=NS(body=body))) is body

    def test_body_none_when_no_message(self) -> None:
        assert ev.get_message_body(NS()) is None


class TestExtractMessageIdLegacy:
    def test_legacy_message_id_attr(self) -> None:
        # Нет .message и нет .body, но есть .message_id напрямую.
        assert ev.extract_message_id(NS(message_id="legacy-7")) == "legacy-7"

    def test_inner_body_without_mid_falls_through_to_legacy(self) -> None:
        # .message.body есть, но mid=None → не возвращаем тут; есть
        # .message_id (legacy) — он и срабатывает.
        sent = NS(message=NS(body=NS(mid=None)), message_id="fallback-9")
        assert ev.extract_message_id(sent) == "fallback-9"

    def test_bare_body_without_mid_falls_through(self) -> None:
        # .body есть, mid=None, нет .message_id → None.
        sent = NS(body=NS(mid=None))
        assert ev.extract_message_id(sent) is None


class TestGetIdsFallback:
    def test_get_ids_exception_uses_fallback_fields(self) -> None:
        # event.get_ids() кидает → используем запасные поля
        # (message.recipient.chat_id + message.sender.user_id).
        def boom():
            raise RuntimeError("broken get_ids")

        e = NS(
            get_ids=boom,
            chat_id=None,
            message=NS(
                recipient=NS(chat_id=500),
                sender=NS(user_id=42),
            ),
        )
        assert ev.get_ids(e) == (500, 42)

    def test_get_chat_id_via_event_chat_id_fallback(self) -> None:
        # Нет get_ids вообще; chat_id прямо на event (форма BotStarted).
        e = NS(chat_id=900)
        assert ev.get_chat_id(e) == 900

    def test_get_ids_via_callback_user_when_no_message(self) -> None:
        e = NS(chat_id=None, message=None, callback=NS(user=NS(user_id=77)))
        assert ev.get_ids(e) == (None, 77)


class TestSendHelpers:
    @pytest.mark.asyncio
    async def test_send_uses_chat_id(self) -> None:
        bot = NS(send_message=AsyncMock(return_value="sent"))
        e = NS(bot=bot, message=NS(recipient=NS(chat_id=100), sender=NS(user_id=42)))
        result = await ev.send(e, "привет", ["kb"])
        assert result == "sent"
        bot.send_message.assert_awaited_once_with(
            chat_id=100, user_id=None, text="привет", attachments=["kb"]
        )

    @pytest.mark.asyncio
    async def test_send_falls_back_to_user_id(self) -> None:
        bot = NS(send_message=AsyncMock())
        e = NS(bot=bot, user=NS(user_id=42))
        await ev.send(e, "личное")
        bot.send_message.assert_awaited_once_with(
            chat_id=None, user_id=42, text="личное", attachments=[]
        )

    @pytest.mark.asyncio
    async def test_send_none_bot_is_noop(self) -> None:
        assert await ev.send(NS(bot=None), "x") is None

    @pytest.mark.asyncio
    async def test_reply_delegates_to_send(self) -> None:
        bot = NS(send_message=AsyncMock(return_value="ok"))
        e = NS(bot=bot, user=NS(user_id=7))
        assert await ev.reply(e, "ответ") == "ok"
        bot.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_to_explicit_target(self) -> None:
        bot = NS(send_message=AsyncMock(return_value="sent"))
        result = await ev.send_to(NS(bot=bot), chat_id=321, text="hi", attachments=["a"])
        assert result == "sent"
        bot.send_message.assert_awaited_once_with(
            chat_id=321, user_id=None, text="hi", attachments=["a"]
        )

    @pytest.mark.asyncio
    async def test_send_to_none_bot_is_noop(self) -> None:
        assert await ev.send_to(NS(bot=None), chat_id=1, text="x") is None


class TestSendOrEditNoBot:
    @pytest.mark.asyncio
    async def test_none_bot_returns_none(self) -> None:
        assert await ev.send_or_edit_screen(NS(bot=None), text="x") is None


class TestAckCallbackNonCallable:
    @pytest.mark.asyncio
    async def test_non_callable_ack_is_noop(self) -> None:
        # event.ack отсутствует/не callable → тихий no-op без падения.
        assert await ev.ack_callback(NS(), "готово") is None
        assert await ev.ack_callback(NS(ack=None), "готово") is None
