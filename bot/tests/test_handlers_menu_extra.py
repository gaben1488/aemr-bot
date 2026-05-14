"""Расширенные тесты handlers/menu.py — экраны согласия, прощания,
emergency/dispatchers/appointment, handle_callback router.

Локально skip без maxapi; в CI работает."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests._helpers import fake_current_user
from tests._helpers import fake_session_scope as _fake_session_scope
from tests._helpers import make_event

pytest.importorskip("maxapi", reason="handlers тесты требуют maxapi")


def _make_event(*, chat_id: int = 100, user_id: int = 42) -> SimpleNamespace:
    # Обёртка над tests/_helpers.make_event. menu-handler'ы проверяют
    # event.callback (None для текстового сообщения, не callback'а) —
    # явно проставляем None, базовая фабрика поле не добавляет.
    event = make_event(chat_id=chat_id, user_id=user_id)
    event.callback = None
    return event


class TestShowConsentStatus:
    """show_consent_status зовёт keyboards.consent_status_keyboard
    (keyboards.py:178, сигнатура `*, consent_active: bool`). Патчим её
    на MagicMock, чтобы изолированно проверить логику ветвления text
    по consent_pdn_at / consent_revoked_at — без create=True, чтобы
    patch упал, если функцию переименуют.
    """
    @pytest.mark.asyncio
    async def test_active_consent_shows_active_text(self) -> None:
        from aemr_bot import keyboards
        from aemr_bot.handlers import menu

        event = _make_event()
        user = SimpleNamespace(
            consent_pdn_at=datetime.now(timezone.utc),
            consent_revoked_at=None,
        )
        with patch.object(keyboards, "consent_status_keyboard",
                          MagicMock(return_value=None)), \
             patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.users_service.get_or_create",
                   AsyncMock(return_value=user)):
            await menu.show_consent_status(event, max_user_id=42)
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        from aemr_bot import texts
        # Активная ветка: текст начинается с неформатируемой части
        # CONSENT_STATUS_ACTIVE (до подстановки даты), не текст других веток.
        assert text.startswith("Согласие на обработку персональных данных дано")
        assert text != texts.CONSENT_STATUS_NEVER

    @pytest.mark.asyncio
    async def test_revoked_consent_shows_revoked_text(self) -> None:
        from aemr_bot import keyboards
        from aemr_bot.handlers import menu

        event = _make_event()
        user = SimpleNamespace(
            consent_pdn_at=None,
            consent_revoked_at=datetime.now(timezone.utc),
        )
        with patch.object(keyboards, "consent_status_keyboard",
                          MagicMock(return_value=None)), \
             patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.users_service.get_or_create",
                   AsyncMock(return_value=user)):
            await menu.show_consent_status(event, max_user_id=42)
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        from aemr_bot import texts
        # Отозванная ветка: характерный фрагмент CONSENT_STATUS_REVOKED.
        assert text.startswith(
            "Согласие на обработку персональных данных отозвано"
        )
        assert "оператор даст по ним финальный ответ" in text
        assert text != texts.CONSENT_STATUS_NEVER

    @pytest.mark.asyncio
    async def test_never_consented_shows_never_text(self) -> None:
        from aemr_bot import keyboards
        from aemr_bot.handlers import menu

        event = _make_event()
        user = SimpleNamespace(consent_pdn_at=None, consent_revoked_at=None)
        with patch.object(keyboards, "consent_status_keyboard",
                          MagicMock(return_value=None)), \
             patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.users_service.get_or_create",
                   AsyncMock(return_value=user)):
            await menu.show_consent_status(event, max_user_id=42)
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        from aemr_bot import texts
        assert text == texts.CONSENT_STATUS_NEVER

    def test_consent_helpers_now_exist(self) -> None:
        """Регрессионная защита: handlers/menu.py обращается к
        keyboards.consent_status_keyboard, .consent_revoke_confirm_keyboard
        и .forget_confirm_keyboard. Раньше отсутствовали — было
        production fail с AttributeError на callback'ах settings:
        consent_status / consent_revoke_ask / forget_ask.

        Сейчас функции есть как алиасы к goodbye_*_keyboard. Тест
        падает если кто-то их случайно удалит.
        """
        from aemr_bot import keyboards

        for name in (
            "consent_status_keyboard",
            "consent_revoke_confirm_keyboard",
            "forget_confirm_keyboard",
        ):
            assert hasattr(keyboards, name), (
                f"keyboards.{name} удалена — тогда callback "
                f"settings:* в handlers/menu.py упадёт с AttributeError"
            )

        # consent_status_keyboard принимает kw-only consent_active=bool
        kb_active = keyboards.consent_status_keyboard(consent_active=True)
        kb_revoked = keyboards.consent_status_keyboard(consent_active=False)
        assert kb_active is not None and kb_revoked is not None


class TestAskForgetConfirm:
    """ask_forget_confirm обращается к keyboards.forget_confirm_keyboard
    (keyboards.py:165). Патчим её на MagicMock для изоляции логики —
    без create=True."""

    @pytest.mark.asyncio
    async def test_no_open_appeals_shows_basic_confirm(self) -> None:
        from aemr_bot import keyboards
        from aemr_bot.handlers import menu

        event = _make_event()
        user = SimpleNamespace(id=1)
        with patch.object(keyboards, "forget_confirm_keyboard",
                          MagicMock(return_value=None)), \
             patch("aemr_bot.handlers.menu.current_user", fake_current_user(user)), \
             patch("aemr_bot.services.appeals.list_unanswered",
                   AsyncMock(return_value=[])):
            await menu.ask_forget_confirm(event)
        event.bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_with_open_appeals_lists_them(self) -> None:
        from aemr_bot import keyboards
        from aemr_bot.handlers import menu

        event = _make_event()
        user = SimpleNamespace(id=1)
        ap = SimpleNamespace(
            id=42,
            user_id=1,
            topic="ЖКХ",
            created_at=datetime.now(timezone.utc),
        )
        with patch.object(keyboards, "forget_confirm_keyboard",
                          MagicMock(return_value=None)), \
             patch("aemr_bot.handlers.menu.current_user", fake_current_user(user)), \
             patch("aemr_bot.services.appeals.list_unanswered",
                   AsyncMock(return_value=[ap])):
            await menu.ask_forget_confirm(event)
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "#42" in text


class TestFormatDtLocal:
    def test_returns_dash_for_none(self) -> None:
        from aemr_bot.handlers.menu import _format_dt_local

        assert _format_dt_local(None) == "—"

    def test_formats_datetime(self) -> None:
        from aemr_bot.handlers.menu import _format_dt_local

        dt = datetime(2026, 5, 10, 12, 30, tzinfo=timezone.utc)
        result = _format_dt_local(dt)
        assert "." in result
        assert ":" in result


class TestAskGoodbye:
    @pytest.mark.asyncio
    async def test_revoke_confirm_sends_text(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        await menu.ask_goodbye_revoke_confirm(event)
        event.bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_erase_confirm_no_open_appeals(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        user = SimpleNamespace(id=1)
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.services.appeals.list_unanswered",
                   AsyncMock(return_value=[])):
            await menu.ask_goodbye_erase_confirm(event)
        event.bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_consent_revoke_confirm_sends(self) -> None:
        from aemr_bot import keyboards
        from aemr_bot.handlers import menu

        event = _make_event()
        with patch.object(keyboards, "consent_revoke_confirm_keyboard",
                          MagicMock(return_value=None)):
            await menu.ask_consent_revoke_confirm(event)
        event.bot.send_message.assert_called_once()


class TestOpenAppointment:
    @pytest.mark.asyncio
    async def test_uses_appointment_text_from_settings(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.settings_store.get",
                   AsyncMock(side_effect=["text-1", "https://e.example"])):
            await menu.open_appointment(event)
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert text == "text-1"

    @pytest.mark.asyncio
    async def test_falls_back_when_text_missing(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.settings_store.get",
                   AsyncMock(return_value=None)):
            await menu.open_appointment(event)
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "скоро" in text


class TestOpenEmergency:
    @pytest.mark.asyncio
    async def test_empty_list_falls_back(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.settings_store.get",
                   AsyncMock(return_value=[])):
            await menu.open_emergency(event)
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "скоро" in text

    @pytest.mark.asyncio
    async def test_groups_by_section(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        contacts = [
            {"section": "Полиция", "name": "ОМВД", "phone": "112"},
            {"section": "Полиция", "name": "ППС", "phone": "02"},
            {"section": "Скорая", "name": "СМП", "phone": "103"},
            # Без section — попадёт в «Прочее».
            {"name": "Энерго", "phone": "123"},
        ]
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.settings_store.get",
                   AsyncMock(return_value=contacts)):
            await menu.open_emergency(event)
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "Полиция" in text
        assert "Скорая" in text
        assert "Прочее" in text
        assert "ОМВД — 112" in text


class TestOpenDispatchers:
    @pytest.mark.asyncio
    async def test_empty_falls_back(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.settings_store.get",
                   AsyncMock(return_value=[])):
            await menu.open_dispatchers(event)
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "скоро" in text

    @pytest.mark.asyncio
    async def test_lists_routes_with_phones(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        items = [
            {"routes": "Маршруты 102, 105", "phone": "8 800 100"},
            {"routes": "Маршруты 110", "phone": "8 800 200"},
        ]
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.settings_store.get",
                   AsyncMock(return_value=items)):
            await menu.open_dispatchers(event)
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "Маршруты 102, 105" in text
        assert "8 800 200" in text


class TestHandleCallback:
    """Маршрутизатор payload-ов меню."""

    @pytest.mark.asyncio
    async def test_menu_main(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        with patch("aemr_bot.handlers.menu.open_main_menu", AsyncMock()) as fn:
            handled = await menu.handle_callback(event, "menu:main", max_user_id=42)
        assert handled is True
        fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_menu_my_appeals(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        with patch("aemr_bot.handlers.menu.open_my_appeals", AsyncMock()) as fn:
            handled = await menu.handle_callback(event, "menu:my_appeals", max_user_id=42)
        assert handled is True
        fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_appeals_page_int(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        with patch("aemr_bot.handlers.menu.open_my_appeals", AsyncMock()) as fn:
            handled = await menu.handle_callback(event, "appeals:page:3", max_user_id=42)
        assert handled is True
        fn.assert_called_once()
        # Проверяем, что page=3 передан
        assert fn.call_args.kwargs.get("page") == 3

    @pytest.mark.asyncio
    async def test_appeals_page_noop(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        with patch("aemr_bot.handlers.menu.open_my_appeals", AsyncMock()) as fn:
            handled = await menu.handle_callback(event, "appeals:page:noop", max_user_id=42)
        assert handled is True
        fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_appeals_page_invalid_int(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        with patch("aemr_bot.handlers.menu.open_my_appeals", AsyncMock()) as fn:
            handled = await menu.handle_callback(event, "appeals:page:zzz", max_user_id=42)
        assert handled is True
        fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_menu_useful_info(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        with patch("aemr_bot.handlers.menu.open_useful_info", AsyncMock()) as fn:
            handled = await menu.handle_callback(event, "menu:useful_info", max_user_id=42)
        assert handled is True
        fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_menu_appointment(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        with patch("aemr_bot.handlers.menu.open_appointment", AsyncMock()) as fn:
            handled = await menu.handle_callback(event, "menu:appointment", max_user_id=42)
        assert handled is True
        fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_menu_settings(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        with patch("aemr_bot.handlers.menu.open_settings", AsyncMock()) as fn:
            handled = await menu.handle_callback(event, "menu:settings", max_user_id=42)
        assert handled is True
        fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_settings_help(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        with patch("aemr_bot.handlers.menu.open_help", AsyncMock()) as fn:
            handled = await menu.handle_callback(event, "settings:help", max_user_id=42)
        assert handled is True
        fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_settings_forget_ask(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        with patch("aemr_bot.handlers.menu.ask_forget_confirm", AsyncMock()) as fn:
            handled = await menu.handle_callback(event, "settings:forget_ask", max_user_id=42)
        assert handled is True
        fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_settings_consent_status(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        with patch("aemr_bot.handlers.menu.show_consent_status", AsyncMock()) as fn:
            handled = await menu.handle_callback(event, "settings:consent_status", max_user_id=42)
        assert handled is True
        fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_settings_consent_revoke_ask(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        with patch("aemr_bot.handlers.menu.ask_consent_revoke_confirm", AsyncMock()) as fn:
            handled = await menu.handle_callback(event, "settings:consent_revoke_ask", max_user_id=42)
        assert handled is True
        fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_settings_consent_revoke_yes(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        with patch("aemr_bot.handlers.menu.do_consent_revoke", AsyncMock()) as fn:
            handled = await menu.handle_callback(event, "settings:consent_revoke_yes", max_user_id=42)
        assert handled is True
        fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_settings_forget_yes(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        with patch("aemr_bot.handlers.menu.do_forget", AsyncMock()) as fn:
            handled = await menu.handle_callback(event, "settings:forget_yes", max_user_id=42)
        assert handled is True
        fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_unknown_payload_returns_false(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        handled = await menu.handle_callback(event, "totally:unknown:payload", max_user_id=42)
        assert handled is False

    # --- max_user_id=None: характеризационные тесты граничных веток ---
    # Исторически handle_callback по-разному ведёт себя без жителя:
    # no-user маршруты работают; menu:my_appeals «съедает» тап (True);
    # остальные user-маршруты и prefix-маршруты проваливаются (False).
    # Эти тесты фиксируют расхождение, чтобы рефактор его не стёр.

    @pytest.mark.asyncio
    async def test_no_user_route_works_without_max_user_id(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        with patch("aemr_bot.handlers.menu.open_main_menu", AsyncMock()) as fn:
            handled = await menu.handle_callback(event, "menu:main", max_user_id=None)
        assert handled is True
        fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_my_appeals_consumed_without_max_user_id(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        with patch("aemr_bot.handlers.menu.open_my_appeals", AsyncMock()) as fn:
            handled = await menu.handle_callback(
                event, "menu:my_appeals", max_user_id=None
            )
        # «Съедает» тап (True), но обработчик не зовёт.
        assert handled is True
        fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_user_required_route_falls_through_without_max_user_id(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        with patch("aemr_bot.handlers.menu.do_forget", AsyncMock()) as fn:
            handled = await menu.handle_callback(
                event, "settings:forget_yes", max_user_id=None
            )
        # User-маршрут без жителя — управление проваливается дальше.
        assert handled is False
        fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_prefix_route_falls_through_without_max_user_id(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        with patch("aemr_bot.handlers.menu.show_appeal", AsyncMock()) as fn:
            handled = await menu.handle_callback(
                event, "appeal:show:5", max_user_id=None
            )
        assert handled is False
        fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_broadcast_unsubscribe_routed_without_dispatcher_ack(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        with patch("aemr_bot.handlers.menu.handle_broadcast_unsubscribe",
                   AsyncMock()) as fn, \
             patch("aemr_bot.handlers.menu.ack_callback", AsyncMock()) as ack:
            handled = await menu.handle_callback(
                event, "broadcast:unsubscribe", max_user_id=42
            )
        assert handled is True
        fn.assert_called_once()
        # ack делегирован внутрь handle_broadcast_unsubscribe — диспетчер
        # сам не акает.
        ack.assert_not_called()

    @pytest.mark.asyncio
    async def test_appeal_show_prefix_parses_id(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        with patch("aemr_bot.handlers.menu.show_appeal", AsyncMock()) as fn:
            handled = await menu.handle_callback(
                event, "appeal:show:77", max_user_id=42
            )
        assert handled is True
        fn.assert_called_once()
        assert fn.call_args.args[1] == 77
