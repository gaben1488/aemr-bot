"""Тесты на handlers/appeal.register() — главный callback/message dispatcher.

Декорированные handlers внутри `register(dp)` нельзя достать напрямую,
поэтому используем MockDispatcher: подсовываем фейковый `dp` с
декораторами, которые сохраняют функцию в атрибуте. Потом вызываем
register(mock_dp), достаём handler и тестируем как обычную coroutine.

Локально skip без maxapi (декораторы Dispatcher).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("maxapi", reason="dispatcher тесты требуют maxapi")


def _make_callback_event(*, chat_id: int = 555, user_id: int = 7,
                         payload: str = "") -> SimpleNamespace:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return SimpleNamespace(
        bot=bot,
        callback=SimpleNamespace(
            payload=payload,
            callback_id="cb-1",
            user=SimpleNamespace(user_id=user_id, first_name="X"),
        ),
        message=SimpleNamespace(
            answer=AsyncMock(),
            sender=SimpleNamespace(user_id=user_id, first_name="X"),
            recipient=SimpleNamespace(chat_id=chat_id),
            body=SimpleNamespace(text="", attachments=[], mid="m-1"),
        ),
    )


def _make_message_event(*, chat_id: int = 555, user_id: int = 7,
                        text: str = "") -> SimpleNamespace:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return SimpleNamespace(
        bot=bot,
        message=SimpleNamespace(
            answer=AsyncMock(),
            sender=SimpleNamespace(user_id=user_id, first_name="X"),
            recipient=SimpleNamespace(chat_id=chat_id),
            body=SimpleNamespace(text=text, attachments=[], mid="m-1"),
        ),
    )


@asynccontextmanager
async def _fake_session_scope():
    yield MagicMock()


class _CapturingDispatcher:
    """Минимальный mock Dispatcher: сохраняет декорированную coroutine
    в атрибуте, чтобы тесты могли её достать."""

    def __init__(self) -> None:
        self.callback_handler = None
        self.message_handler = None

    def message_callback(self):
        def deco(fn):
            self.callback_handler = fn
            return fn
        return deco

    def message_created(self):
        def deco(fn):
            self.message_handler = fn
            return fn
        return deco


@pytest.fixture
def captured_handlers():
    """Регистрирует appeal.register на mock dp и возвращает (callback, message)."""
    from aemr_bot.handlers import appeal

    dp = _CapturingDispatcher()
    appeal.register(dp)
    assert dp.callback_handler is not None
    assert dp.message_handler is not None
    return dp.callback_handler, dp.message_handler


# --- on_callback ветви --------------------------------------------------------


class TestCallbackBasics:
    @pytest.mark.asyncio
    async def test_no_user_id_returns(self, captured_handlers) -> None:
        on_callback, _ = captured_handlers
        event = _make_callback_event(payload="menu:new_appeal", user_id=None)
        # get_user_id возвращает None — handler logs warning, return
        with patch("aemr_bot.handlers.appeal.get_user_id", return_value=None):
            await on_callback(event)
        # Никаких сторонних вызовов не сработало
        event.bot.send_message.assert_not_called()


class TestCallbackAdminChatGuard:
    @pytest.mark.asyncio
    async def test_user_callback_in_admin_chat_silently_ack(
        self, captured_handlers
    ) -> None:
        """Если жительский callback (например menu:new_appeal) пришёл из
        админ-группы — это сценарий бага, ack и игнорим."""
        on_callback, _ = captured_handlers
        event = _make_callback_event(payload="menu:new_appeal", chat_id=123)
        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 123), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await on_callback(event)
        event.bot.send_message.assert_not_called()


class TestCallbackConsent:
    @pytest.mark.asyncio
    async def test_consent_yes_sets_consent_and_asks_contact(
        self, captured_handlers
    ) -> None:
        on_callback, _ = captured_handlers
        event = _make_callback_event(payload="consent:yes")
        set_consent = AsyncMock()
        ask_contact = AsyncMock()
        notify = AsyncMock()
        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 999), \
             patch("aemr_bot.handlers.appeal.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.appeal.users_service.set_consent",
                   set_consent), \
             patch("aemr_bot.handlers.appeal.admin_events.notify_consent_given",
                   notify), \
             patch("aemr_bot.handlers.appeal.appeal_funnel.ask_contact_or_skip",
                   ask_contact), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await on_callback(event)
        set_consent.assert_called_once()
        notify.assert_called_once_with(event.bot, max_user_id=7)
        ask_contact.assert_called_once()

    @pytest.mark.asyncio
    async def test_consent_no_resets_and_returns_to_menu(
        self, captured_handlers
    ) -> None:
        on_callback, _ = captured_handlers
        event = _make_callback_event(payload="consent:no")
        reset = AsyncMock()
        open_menu = AsyncMock()
        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 999), \
             patch("aemr_bot.handlers.appeal.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.appeal.users_service.reset_state", reset), \
             patch("aemr_bot.handlers.appeal.drop_user_lock"), \
             patch("aemr_bot.handlers.menu.open_main_menu", open_menu), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await on_callback(event)
        reset.assert_called_once()
        open_menu.assert_called_once()
        event.bot.send_message.assert_called_once()


class TestCallbackCancel:
    @pytest.mark.asyncio
    async def test_cancel_resets_and_back_to_menu(
        self, captured_handlers
    ) -> None:
        on_callback, _ = captured_handlers
        event = _make_callback_event(payload="cancel")
        reset = AsyncMock()
        open_menu = AsyncMock()
        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 999), \
             patch("aemr_bot.handlers.appeal.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.appeal.users_service.reset_state", reset), \
             patch("aemr_bot.handlers.appeal.drop_user_lock"), \
             patch("aemr_bot.handlers.menu.open_main_menu", open_menu), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await on_callback(event)
        reset.assert_called_once()
        open_menu.assert_called_once()


class TestCallbackAddrReuse:
    @pytest.mark.asyncio
    async def test_addr_reuse_with_prev(self, captured_handlers) -> None:
        on_callback, _ = captured_handlers
        event = _make_callback_event(payload="addr:reuse")
        ask_topic = AsyncMock()
        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 999), \
             patch("aemr_bot.handlers.appeal.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.appeal.users_service.get_or_create",
                   AsyncMock(return_value=SimpleNamespace(id=1))), \
             patch("aemr_bot.handlers.appeal.appeals_service.find_last_address_for_user",
                   AsyncMock(return_value=("Елизовское ГП", "Ленина 1"))), \
             patch("aemr_bot.handlers.appeal.users_service.set_state", AsyncMock()), \
             patch("aemr_bot.handlers.appeal.appeal_funnel.ask_topic", ask_topic), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await on_callback(event)
        ask_topic.assert_called_once()

    @pytest.mark.asyncio
    async def test_addr_reuse_without_prev_falls_back(
        self, captured_handlers
    ) -> None:
        on_callback, _ = captured_handlers
        event = _make_callback_event(payload="addr:reuse")
        ask_locality = AsyncMock()
        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 999), \
             patch("aemr_bot.handlers.appeal.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.appeal.users_service.get_or_create",
                   AsyncMock(return_value=SimpleNamespace(id=1))), \
             patch("aemr_bot.handlers.appeal.appeals_service.find_last_address_for_user",
                   AsyncMock(return_value=None)), \
             patch("aemr_bot.handlers.appeal.appeal_funnel.ask_locality",
                   ask_locality), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await on_callback(event)
        ask_locality.assert_called_once()


class TestCallbackAddrNew:
    @pytest.mark.asyncio
    async def test_addr_new_calls_ask_locality(self, captured_handlers) -> None:
        on_callback, _ = captured_handlers
        event = _make_callback_event(payload="addr:new")
        ask_locality = AsyncMock()
        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 999), \
             patch("aemr_bot.handlers.appeal.appeal_funnel.ask_locality",
                   ask_locality), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await on_callback(event)
        ask_locality.assert_called_once()


class TestCallbackLocality:
    @pytest.mark.asyncio
    async def test_locality_valid_idx(self, captured_handlers) -> None:
        on_callback, _ = captured_handlers
        event = _make_callback_event(payload="locality:0")
        ask_address = AsyncMock()
        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 999), \
             patch("aemr_bot.handlers.appeal.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.appeal.settings_store.get",
                   AsyncMock(return_value=["Елизовское ГП"])), \
             patch("aemr_bot.handlers.appeal.users_service.update_dialog_data",
                   AsyncMock()), \
             patch("aemr_bot.handlers.appeal.appeal_funnel.ask_address",
                   ask_address), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await on_callback(event)
        ask_address.assert_called_once()

    @pytest.mark.asyncio
    async def test_locality_invalid_idx_returns(self, captured_handlers) -> None:
        on_callback, _ = captured_handlers
        event = _make_callback_event(payload="locality:abc")
        ask_address = AsyncMock()
        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 999), \
             patch("aemr_bot.handlers.appeal.appeal_funnel.ask_address",
                   ask_address), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await on_callback(event)
        ask_address.assert_not_called()

    @pytest.mark.asyncio
    async def test_locality_out_of_range_logs_warning(
        self, captured_handlers
    ) -> None:
        on_callback, _ = captured_handlers
        event = _make_callback_event(payload="locality:99")
        ask_address = AsyncMock()
        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 999), \
             patch("aemr_bot.handlers.appeal.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.appeal.settings_store.get",
                   AsyncMock(return_value=["Елизовское ГП"])), \
             patch("aemr_bot.handlers.appeal.appeal_funnel.ask_address",
                   ask_address), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await on_callback(event)
        ask_address.assert_not_called()


class TestCallbackGeo:
    @pytest.mark.asyncio
    async def test_geo_confirm_with_address_edits_current_card(
        self, captured_handlers
    ) -> None:
        on_callback, _ = captured_handlers
        event = _make_callback_event(payload="geo:confirm")
        user = SimpleNamespace(
            dialog_state="awaiting_geo_confirm",
            dialog_data={
                "locality": "Елизовское ГП",
                "detected_locality": "Елизовское ГП",
                "detected_street": "Ленина",
                "detected_house_number": "5",
                "progress_message_id": "m-geo",
            },
        )
        ask_topic = AsyncMock()

        @asynccontextmanager
        async def fake_scope():
            yield AsyncMock()

        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 999), \
             patch("aemr_bot.handlers.appeal.session_scope", fake_scope), \
             patch("aemr_bot.handlers.appeal.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.appeal.appeal_funnel.ask_topic",
                   ask_topic), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await on_callback(event)

        ask_topic.assert_called_once_with(event, 7)
        assert user.dialog_data["progress_message_id"] == "m-geo"

    @pytest.mark.asyncio
    async def test_geo_edit_address_keeps_progress_card(
        self, captured_handlers
    ) -> None:
        on_callback, _ = captured_handlers
        event = _make_callback_event(payload="geo:edit_address")
        user = SimpleNamespace(
            dialog_state="awaiting_geo_confirm",
            dialog_data={
                "locality": "Елизовское ГП",
                "detected_locality": "Елизовское ГП",
                "detected_street": "Ленина",
                "progress_message_id": "m-geo",
            },
        )
        ask_address = AsyncMock()

        @asynccontextmanager
        async def fake_scope():
            yield AsyncMock()

        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 999), \
             patch("aemr_bot.handlers.appeal.session_scope", fake_scope), \
             patch("aemr_bot.handlers.appeal.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.appeal.appeal_funnel.ask_address",
                   ask_address), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await on_callback(event)

        ask_address.assert_called_once_with(event, 7)
        assert user.dialog_data["progress_message_id"] == "m-geo"

    @pytest.mark.asyncio
    async def test_geo_other_locality_keeps_progress_card(
        self, captured_handlers
    ) -> None:
        on_callback, _ = captured_handlers
        event = _make_callback_event(payload="geo:other_locality")
        user = SimpleNamespace(
            dialog_state="awaiting_geo_confirm",
            dialog_data={
                "locality": "Елизовское ГП",
                "detected_locality": "Елизовское ГП",
                "detected_street": "Ленина",
                "progress_message_id": "m-geo",
            },
        )
        ask_locality = AsyncMock()

        @asynccontextmanager
        async def fake_scope():
            yield AsyncMock()

        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 999), \
             patch("aemr_bot.handlers.appeal.session_scope", fake_scope), \
             patch("aemr_bot.handlers.appeal.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.appeal.appeal_funnel.ask_locality",
                   ask_locality), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await on_callback(event)

        ask_locality.assert_called_once_with(event, 7)
        assert user.dialog_data["progress_message_id"] == "m-geo"


class TestCallbackTopic:
    @pytest.mark.asyncio
    async def test_topic_valid_idx(self, captured_handlers) -> None:
        on_callback, _ = captured_handlers
        event = _make_callback_event(payload="topic:0")
        ask_summary = AsyncMock()
        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 999), \
             patch("aemr_bot.handlers.appeal.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.appeal.settings_store.get",
                   AsyncMock(return_value=["Дороги"])), \
             patch("aemr_bot.handlers.appeal.users_service.update_dialog_data",
                   AsyncMock()), \
             patch("aemr_bot.handlers.appeal.appeal_funnel.ask_summary",
                   ask_summary), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await on_callback(event)
        ask_summary.assert_called_once()


class TestCallbackAppealSubmit:
    @pytest.mark.asyncio
    async def test_appeal_submit_finalizes(self, captured_handlers) -> None:
        on_callback, _ = captured_handlers
        event = _make_callback_event(payload="appeal:submit")
        finalize = AsyncMock()
        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 999), \
             patch("aemr_bot.handlers.appeal.appeal_funnel.finalize_appeal",
                   finalize), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await on_callback(event)
        finalize.assert_called_once()


class TestCallbackFallthroughToMenu:
    @pytest.mark.asyncio
    async def test_unknown_payload_routes_to_menu_handler(
        self, captured_handlers
    ) -> None:
        """Незнакомый callback (например 'menu:about') не обрабатывается
        in-flow, идёт в handlers/menu.handle_callback."""
        on_callback, _ = captured_handlers
        event = _make_callback_event(payload="menu:about")
        menu_handle = AsyncMock()
        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 999), \
             patch("aemr_bot.handlers.menu.handle_callback", menu_handle):
            await on_callback(event)
        menu_handle.assert_called_once()


# --- on_message ветви ---------------------------------------------------------


class TestMessageNoChatId:
    @pytest.mark.asyncio
    async def test_no_chat_id_logs_and_returns(
        self, captured_handlers
    ) -> None:
        _, on_message = captured_handlers
        event = _make_message_event()
        with patch("aemr_bot.handlers.appeal.get_chat_id", return_value=None):
            await on_message(event)
        event.bot.send_message.assert_not_called()


class TestMessageAdminCancel:
    @pytest.mark.asyncio
    async def test_cancel_in_admin_chat_clears_wizards(
        self, captured_handlers
    ) -> None:
        _, on_message = captured_handlers
        event = _make_message_event(chat_id=123, text="/cancel")
        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 123), \
             patch("aemr_bot.handlers.appeal.get_chat_id", return_value=123), \
             patch("aemr_bot.handlers.appeal.get_message_text",
                   return_value="/cancel"), \
             patch("aemr_bot.handlers.appeal.get_message_body",
                   return_value=event.message.body), \
             patch("aemr_bot.handlers.broadcast._wizards", {7: {"step": "x"}}), \
             patch("aemr_bot.handlers.admin_commands._op_wizards",
                   {7: {"step": "x"}}), \
             patch("aemr_bot.handlers.operator_reply.drop_reply_intent"):
            await on_message(event)
        event.bot.send_message.assert_called_once()
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "сброшены" in text


class TestMessageCitizenUnknownCommand:
    @pytest.mark.asyncio
    async def test_operator_only_command_warned(
        self, captured_handlers
    ) -> None:
        _, on_message = captured_handlers
        event = _make_message_event(chat_id=42, text="/reply")
        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 999), \
             patch("aemr_bot.handlers.appeal.get_chat_id", return_value=42), \
             patch("aemr_bot.handlers.appeal.get_message_text",
                   return_value="/reply"), \
             patch("aemr_bot.handlers.appeal.get_message_body",
                   return_value=event.message.body):
            await on_message(event)
        # Жителю объясняем, что /reply — только для операторов
        event.message.answer.assert_called_once()
        text = event.message.answer.call_args.args[0]
        assert "только в служебной" in text

    @pytest.mark.asyncio
    async def test_unknown_command_warned(self, captured_handlers) -> None:
        _, on_message = captured_handlers
        event = _make_message_event(chat_id=42, text="/foo")
        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 999), \
             patch("aemr_bot.handlers.appeal.get_chat_id", return_value=42), \
             patch("aemr_bot.handlers.appeal.get_message_text",
                   return_value="/foo"), \
             patch("aemr_bot.handlers.appeal.get_message_body",
                   return_value=event.message.body):
            await on_message(event)
        text = event.message.answer.call_args.args[0]
        assert "/foo" in text
        assert "не распознана" in text

    @pytest.mark.asyncio
    async def test_known_citizen_command_passes_silently(
        self, captured_handlers
    ) -> None:
        """Известная команда жителя (`/start`, `/menu` …) не реагируем
        на этом уровне — она обрабатывается отдельным @Command handler.
        Здесь должно быть тихо, без ошибки."""
        _, on_message = captured_handlers
        event = _make_message_event(chat_id=42, text="/start")
        with patch("aemr_bot.handlers.appeal.cfg.admin_group_id", 999), \
             patch("aemr_bot.handlers.appeal.get_chat_id", return_value=42), \
             patch("aemr_bot.handlers.appeal.get_message_text",
                   return_value="/start"), \
             patch("aemr_bot.handlers.appeal.get_message_body",
                   return_value=event.message.body):
            await on_message(event)
        event.message.answer.assert_not_called()
