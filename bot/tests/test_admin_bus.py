"""Тесты единой admin_bus и AdminChatActivityMiddleware.

SACRED-fix: гарантирует что любой outgoing/incoming в admin chat
синхронизирует menu_tracker. Без этого freshness-rule в
admin_card.render и send_or_edit_screen врёт «эта карточка ещё
последняя», когда выше в чате уже лежат pulse / ответы операторов.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


pytest.importorskip("maxapi", reason="нужен maxapi для admin_bus импорта")


@pytest.fixture(autouse=True)
def _clean_tracker():
    from aemr_bot.utils import menu_tracker

    menu_tracker.clear_all()
    yield
    menu_tracker.clear_all()


class TestAdminBusSend:
    @pytest.mark.asyncio
    async def test_send_advances_tracker_on_success(self) -> None:
        from aemr_bot.services import admin_bus
        from aemr_bot.utils import menu_tracker

        bot = MagicMock()
        bot.send_message = AsyncMock(
            return_value=SimpleNamespace(
                message=SimpleNamespace(body=SimpleNamespace(mid="msg-7"))
            )
        )
        with patch("aemr_bot.config.settings.admin_group_id", 555):
            mid = await admin_bus.send(bot, text="test pulse")
        assert mid == "msg-7"
        assert menu_tracker.get_last_menu_mid(555) == "msg-7"
        bot.send_message.assert_awaited_once()
        kwargs = bot.send_message.await_args.kwargs
        assert kwargs.get("chat_id") == 555
        assert kwargs.get("text") == "test pulse"

    @pytest.mark.asyncio
    async def test_send_no_admin_group_returns_none(self) -> None:
        from aemr_bot.services import admin_bus
        from aemr_bot.utils import menu_tracker

        bot = MagicMock()
        bot.send_message = AsyncMock()
        with patch("aemr_bot.config.settings.admin_group_id", 0):
            mid = await admin_bus.send(bot, text="lost")
        assert mid is None
        bot.send_message.assert_not_called()
        assert menu_tracker.get_last_menu_mid(0) is None

    @pytest.mark.asyncio
    async def test_send_failure_no_tracker_advance(self) -> None:
        """Если bot.send_message упал — tracker НЕ должен двигаться
        (иначе следующий freshness-check возьмёт mid сообщения которого
        реально нет в чате)."""
        from aemr_bot.services import admin_bus
        from aemr_bot.utils import menu_tracker

        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=RuntimeError("MAX 500"))
        menu_tracker.set_last_menu_mid(555, "before-fail")
        with patch("aemr_bot.config.settings.admin_group_id", 555):
            mid = await admin_bus.send(bot, text="upset")
        assert mid is None
        assert menu_tracker.get_last_menu_mid(555) == "before-fail"

    @pytest.mark.asyncio
    async def test_send_with_attachments_passes_through(self) -> None:
        from aemr_bot.services import admin_bus

        bot = MagicMock()
        bot.send_message = AsyncMock(
            return_value=SimpleNamespace(
                message=SimpleNamespace(body=SimpleNamespace(mid="a-1"))
            )
        )
        attachments = [{"type": "image", "payload": {"url": "x"}}]
        with patch("aemr_bot.config.settings.admin_group_id", 555):
            await admin_bus.send(bot, text="t", attachments=attachments)
        kwargs = bot.send_message.await_args.kwargs
        assert kwargs.get("attachments") == attachments

    @pytest.mark.asyncio
    async def test_send_with_link_passes_through(self) -> None:
        from aemr_bot.services import admin_bus

        bot = MagicMock()
        bot.send_message = AsyncMock(
            return_value=SimpleNamespace(
                message=SimpleNamespace(body=SimpleNamespace(mid="r-1"))
            )
        )
        fake_link = SimpleNamespace(type="reply", mid="quoted")
        with patch("aemr_bot.config.settings.admin_group_id", 555):
            await admin_bus.send(bot, text="reply text", link=fake_link)
        kwargs = bot.send_message.await_args.kwargs
        assert kwargs.get("link") is fake_link


class TestNoteIncomingAdminMessage:
    def test_advances_tracker_on_valid_mid(self) -> None:
        from aemr_bot.services import admin_bus
        from aemr_bot.utils import menu_tracker

        with patch("aemr_bot.config.settings.admin_group_id", 555):
            admin_bus.note_incoming_admin_message("operator-msg-9")
        assert menu_tracker.get_last_menu_mid(555) == "operator-msg-9"

    def test_no_admin_group_noop(self) -> None:
        from aemr_bot.services import admin_bus
        from aemr_bot.utils import menu_tracker

        with patch("aemr_bot.config.settings.admin_group_id", 0):
            admin_bus.note_incoming_admin_message("foo")
        assert menu_tracker.get_last_menu_mid(0) is None

    def test_none_mid_noop(self) -> None:
        """Если mid не извлёкся (None) — не падаем, tracker не двигаем."""
        from aemr_bot.services import admin_bus
        from aemr_bot.utils import menu_tracker

        menu_tracker.set_last_menu_mid(555, "before")
        with patch("aemr_bot.config.settings.admin_group_id", 555):
            admin_bus.note_incoming_admin_message(None)
        assert menu_tracker.get_last_menu_mid(555) == "before"


class TestAdminChatActivityMiddleware:
    """Outer middleware: incoming MessageCreated в admin chat → tracker.

    После 2026-05-27 (fix root-cause): middleware строго проверяет
    `isinstance(event_object, MessageCreated)`. MessageCallback
    игнорируется — иначе tracker съезжал бы на mid старой карточки,
    на которой нажали кнопку, и следующий freshness-check ошибочно
    edit'ил бы эту карточку поверх sacred-инварианта.
    """

    def _make_message_created(self, *, chat_id: int, mid: str):
        """Создать Mock с spec=MessageCreated — isinstance вернёт True."""
        from maxapi.types import MessageCreated
        from unittest.mock import MagicMock

        event = MagicMock(spec=MessageCreated)
        event.message = SimpleNamespace(
            recipient=SimpleNamespace(chat_id=chat_id),
            body=SimpleNamespace(mid=mid),
        )
        return event

    @pytest.mark.asyncio
    async def test_advances_tracker_on_admin_chat_message(self) -> None:
        from aemr_bot.handlers import AdminChatActivityMiddleware
        from aemr_bot.utils import menu_tracker

        mw = AdminChatActivityMiddleware()
        event = self._make_message_created(chat_id=555, mid="op-text-42")
        handler = AsyncMock(return_value="handler-result")
        with patch("aemr_bot.config.settings.admin_group_id", 555):
            result = await mw(handler, event, {})
        assert result == "handler-result"
        assert menu_tracker.get_last_menu_mid(555) == "op-text-42"

    @pytest.mark.asyncio
    async def test_skips_non_admin_chat(self) -> None:
        from aemr_bot.handlers import AdminChatActivityMiddleware
        from aemr_bot.utils import menu_tracker

        mw = AdminChatActivityMiddleware()
        event = self._make_message_created(chat_id=42, mid="citizen-msg")
        handler = AsyncMock(return_value="ok")
        with patch("aemr_bot.config.settings.admin_group_id", 555):
            await mw(handler, event, {})
        # tracker для 555 не тронут, для 42 тоже (это не admin)
        assert menu_tracker.get_last_menu_mid(555) is None
        assert menu_tracker.get_last_menu_mid(42) is None

    @pytest.mark.asyncio
    async def test_no_admin_group_id_skips(self) -> None:
        from aemr_bot.handlers import AdminChatActivityMiddleware

        mw = AdminChatActivityMiddleware()
        event = self._make_message_created(chat_id=555, mid="m-x")
        handler = AsyncMock(return_value="ok")
        # admin_group_id не задан → middleware no-op, handler всё равно
        # вызывается
        with patch("aemr_bot.config.settings.admin_group_id", 0):
            result = await mw(handler, event, {})
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_handler_called_even_when_tracker_sync_fails(self) -> None:
        """Tracker-sync — best-effort. Любая ошибка внутри не должна
        мешать handler'у обработать событие. Здесь используем broken
        MessageCreated без body — внутренний try/except должен поглотить."""
        from aemr_bot.handlers import AdminChatActivityMiddleware
        from maxapi.types import MessageCreated
        from unittest.mock import MagicMock

        mw = AdminChatActivityMiddleware()
        event = MagicMock(spec=MessageCreated)
        event.message = None  # broken
        handler = AsyncMock(return_value="ok-anyway")
        with patch("aemr_bot.config.settings.admin_group_id", 555):
            result = await mw(handler, event, {})
        assert result == "ok-anyway"

    @pytest.mark.asyncio
    async def test_callback_event_does_not_move_tracker(self) -> None:
        """ROOT CAUSE fix-test: MessageCallback (не MessageCreated)
        ДОЛЖЕН быть проигнорирован middleware'ом. До fix тут tracker
        съезжал бы на mid старой карточки."""
        from aemr_bot.handlers import AdminChatActivityMiddleware
        from aemr_bot.utils import menu_tracker

        menu_tracker.set_last_menu_mid(555, "menu-existing-1")

        mw = AdminChatActivityMiddleware()
        # Plain SimpleNamespace — НЕ MessageCreated. Имитация MessageCallback.
        event = SimpleNamespace(
            message=SimpleNamespace(
                recipient=SimpleNamespace(chat_id=555),
                body=SimpleNamespace(mid="old-card-mid"),
            ),
            callback=SimpleNamespace(callback_id="cb"),
        )
        handler = AsyncMock(return_value="ok")
        with patch("aemr_bot.config.settings.admin_group_id", 555):
            await mw(handler, event, {})
        # Tracker не сдвинулся — это и есть смысл fix'а.
        assert menu_tracker.get_last_menu_mid(555) == "menu-existing-1"
