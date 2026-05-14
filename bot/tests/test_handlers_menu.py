"""Тесты handlers/menu.py — навигация по меню жителя.

handlers/__init__.py делает `from maxapi import Dispatcher`, без maxapi
модуль не импортируется. Локально skip, в CI работает.

Покрываем:
- open_main_menu: блокированный житель vs обычный
- open_my_appeals: пустой список, пагинация
- start_appeal_followup: аппил не принадлежит жителю, аппил CLOSED
- start_appeal_repeat: со старым адресом, без адреса (fallback в воронку)
- show_appeal: not found
- open_useful_info, open_settings, open_help, open_goodbye — smoke
- do_subscribe: блокирован, нет mini-consent, уже подписан
- do_unsubscribe: блокирован, не подписан, обычный
- handle_broadcast_unsubscribe: уже не подписан
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("maxapi", reason="handlers тесты требуют maxapi")


def _make_event(*, chat_id: int = 100, user_id: int = 42) -> SimpleNamespace:
    """Минимальный mock event с .bot.send_message + .message.answer."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return SimpleNamespace(
        bot=bot,
        message=SimpleNamespace(
            answer=AsyncMock(),
            sender=SimpleNamespace(user_id=user_id),
            recipient=SimpleNamespace(chat_id=chat_id),
            body=SimpleNamespace(text="", attachments=[], mid="m-1"),
        ),
    )


def _make_callback_event(*, chat_id: int = 100, user_id: int = 42) -> SimpleNamespace:
    """Callback-event: есть исходный mid, поэтому меню должно редактироваться."""
    event = _make_event(chat_id=chat_id, user_id=user_id)
    event.bot.edit_message = AsyncMock()
    event.callback = SimpleNamespace(
        payload="",
        callback_id="cb-1",
        user=SimpleNamespace(user_id=user_id),
    )
    event.ack = AsyncMock()
    return event


@asynccontextmanager
async def _fake_session_scope():
    """asynccontextmanager-mock для session_scope, отдаёт MagicMock как сессию."""
    yield MagicMock()


class TestOpenMainMenu:
    @pytest.mark.asyncio
    async def test_blocked_user_gets_blocked_menu(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        user = SimpleNamespace(is_blocked=True)
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.menu.settings_store.get",
                   AsyncMock(return_value="https://reception")), \
             patch("aemr_bot.handlers.menu.broadcasts_service.is_subscribed",
                   AsyncMock(return_value=False)):
            await menu.open_main_menu(event)
        event.bot.send_message.assert_called_once()
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "заблокирован" in text.lower()

    @pytest.mark.asyncio
    async def test_normal_user_gets_main_menu(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        user = SimpleNamespace(is_blocked=False)
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.menu.broadcasts_service.is_subscribed",
                   AsyncMock(return_value=True)):
            await menu.open_main_menu(event)
        event.bot.send_message.assert_called_once()


class TestOpenMyAppeals:
    @pytest.mark.asyncio
    async def test_empty_list_shows_empty_text(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        user = SimpleNamespace(id=1, is_blocked=False)
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.menu.appeals_service.count_for_user",
                   AsyncMock(return_value=0)):
            await menu.open_my_appeals(event, max_user_id=42)
        event.bot.send_message.assert_called_once()
        from aemr_bot import texts
        assert event.bot.send_message.call_args.kwargs.get("text") == texts.APPEAL_LIST_EMPTY

    @pytest.mark.asyncio
    async def test_renders_first_page_with_total(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        user = SimpleNamespace(id=1, is_blocked=False)
        # 12 обращений → 3 страницы по 5
        appeals_mock = []
        for i in range(5):
            ap = MagicMock()
            ap.id = i
            appeals_mock.append(ap)
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.menu.appeals_service.count_for_user",
                   AsyncMock(return_value=12)), \
             patch("aemr_bot.handlers.menu.appeals_service.list_for_user",
                   AsyncMock(return_value=appeals_mock)), \
             patch("aemr_bot.handlers.menu.card_format.appeal_list_label",
                   side_effect=lambda a: f"label-{a.id}"):
            await menu.open_my_appeals(event, max_user_id=42, page=1)
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "стр. 1/3" in text
        assert "всего 12" in text


class TestStartAppealFollowup:
    @pytest.mark.asyncio
    async def test_appeal_not_found(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.appeals_service.get_by_id",
                   AsyncMock(return_value=None)):
            await menu.start_appeal_followup(event, appeal_id=99, max_user_id=42)
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "не найдено" in text

    @pytest.mark.asyncio
    async def test_appeal_not_owned_by_user(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        appeal = MagicMock()
        appeal.user.max_user_id = 999  # другой житель
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.appeals_service.get_by_id",
                   AsyncMock(return_value=appeal)):
            await menu.start_appeal_followup(event, appeal_id=1, max_user_id=42)
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "не найдено" in text

    @pytest.mark.asyncio
    async def test_closed_appeal_blocks_followup(self) -> None:
        from aemr_bot.db.models import AppealStatus
        from aemr_bot.handlers import menu

        event = _make_event()
        appeal = MagicMock()
        appeal.user.max_user_id = 42
        appeal.status = AppealStatus.CLOSED.value
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.appeals_service.get_by_id",
                   AsyncMock(return_value=appeal)):
            await menu.start_appeal_followup(event, appeal_id=1, max_user_id=42)
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "закрыто" in text.lower() or "Подать похожее" in text

    @pytest.mark.asyncio
    async def test_answered_appeal_blocks_followup_and_points_to_repeat(self) -> None:
        from aemr_bot.db.models import AppealStatus
        from aemr_bot.handlers import menu

        event = _make_event()
        appeal = MagicMock()
        appeal.user.max_user_id = 42
        appeal.status = AppealStatus.ANSWERED.value
        set_state = AsyncMock()
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.appeals_service.get_by_id",
                   AsyncMock(return_value=appeal)), \
             patch("aemr_bot.handlers.menu.users_service.set_state", set_state):
            await menu.start_appeal_followup(event, appeal_id=1, max_user_id=42)

        set_state.assert_not_called()
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "новое" in text.lower()
        assert "Подать похожее" in text


class TestStartAppealRepeat:
    @pytest.mark.asyncio
    async def test_appeal_not_found(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.appeals_service.get_by_id",
                   AsyncMock(return_value=None)):
            await menu.start_appeal_repeat(event, appeal_id=1, max_user_id=42)
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "не найдено" in text

    @pytest.mark.asyncio
    async def test_falls_back_to_full_funnel_without_address(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        appeal = MagicMock()
        appeal.user.max_user_id = 42
        appeal.locality = None
        appeal.address = None
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.appeals_service.get_by_id",
                   AsyncMock(return_value=appeal)), \
             patch("aemr_bot.handlers.appeal_funnel.start_appeal_flow",
                   AsyncMock()) as start_flow:
            await menu.start_appeal_repeat(event, appeal_id=1, max_user_id=42)
        start_flow.assert_called_once()

    @pytest.mark.asyncio
    async def test_answered_repeat_marks_dialog_data(self) -> None:
        from aemr_bot.db.models import AppealStatus, DialogState
        from aemr_bot.handlers import menu

        event = _make_event()
        appeal = MagicMock()
        appeal.id = 7
        appeal.user.max_user_id = 42
        appeal.locality = "Елизовское ГП"
        appeal.address = "Ленина, 1"
        appeal.topic = "Дороги"
        appeal.status = AppealStatus.ANSWERED.value
        set_state = AsyncMock()
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.appeals_service.get_by_id",
                   AsyncMock(return_value=appeal)), \
             patch("aemr_bot.handlers.menu.users_service.set_state", set_state):
            await menu.start_appeal_repeat(event, appeal_id=7, max_user_id=42)

        set_state.assert_called_once()
        args = set_state.call_args.args
        kwargs = set_state.call_args.kwargs
        assert args[2] == DialogState.AWAITING_SUMMARY
        data = kwargs["data"]
        assert data["repeat_source_appeal_id"] == 7
        assert data["repeat_source_status"] == AppealStatus.ANSWERED.value


class TestShowAppeal:
    @pytest.mark.asyncio
    async def test_not_found_responds(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.appeals_service.get_by_id",
                   AsyncMock(return_value=None)):
            await menu.show_appeal(event, appeal_id=1, max_user_id=42)
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "не найдено" in text


class TestSubscribeFlow:
    @pytest.mark.asyncio
    async def test_blocked_user_cannot_subscribe(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        user = SimpleNamespace(
            is_blocked=True, consent_broadcast_at=None
        )
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.users_service.get_or_create",
                   AsyncMock(return_value=user)):
            await menu.do_subscribe(event, max_user_id=42)
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "заблокирован" in text.lower()

    @pytest.mark.asyncio
    async def test_no_consent_shows_mini_consent(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        user = SimpleNamespace(is_blocked=False, consent_broadcast_at=None)
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.users_service.get_or_create",
                   AsyncMock(return_value=user)):
            await menu.do_subscribe(event, max_user_id=42)
        from aemr_bot import texts
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert text == texts.SUBSCRIBE_MINI_CONSENT

    @pytest.mark.asyncio
    async def test_already_subscribed_idempotent(self) -> None:
        from datetime import datetime, timezone

        from aemr_bot.handlers import menu

        event = _make_event()
        user = SimpleNamespace(
            is_blocked=False, consent_broadcast_at=datetime.now(timezone.utc)
        )
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.menu.broadcasts_service.is_subscribed",
                   AsyncMock(return_value=True)):
            await menu.do_subscribe(event, max_user_id=42)
        from aemr_bot import texts
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert text == texts.SUBSCRIBE_ALREADY_ON

    @pytest.mark.asyncio
    async def test_subscribes_when_consent_ok(self) -> None:
        from datetime import datetime, timezone

        from aemr_bot.handlers import menu

        event = _make_event()
        user = SimpleNamespace(
            is_blocked=False, consent_broadcast_at=datetime.now(timezone.utc)
        )
        set_sub = AsyncMock()
        notify = AsyncMock()
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.menu.broadcasts_service.is_subscribed",
                   AsyncMock(return_value=False)), \
             patch("aemr_bot.handlers.menu.broadcasts_service.set_subscription",
                   set_sub), \
             patch("aemr_bot.handlers.menu.admin_events.notify_broadcast_subscribed",
                   notify):
            await menu.do_subscribe(event, max_user_id=42)
        set_sub.assert_called_once()
        notify.assert_called_once_with(event.bot, max_user_id=42)

    @pytest.mark.asyncio
    async def test_subscribe_confirm_records_and_notifies(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        session = AsyncMock()
        user = SimpleNamespace(is_blocked=False)
        notify = AsyncMock()

        @asynccontextmanager
        async def fake_scope():
            yield session

        with patch("aemr_bot.handlers.menu.session_scope", fake_scope), \
             patch("aemr_bot.handlers.menu.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.services.operators.write_audit", AsyncMock()), \
             patch("aemr_bot.handlers.menu.admin_events.notify_broadcast_subscribed",
                   notify):
            await menu.do_subscribe_confirm(event, max_user_id=42)

        session.execute.assert_called_once()
        notify.assert_called_once_with(event.bot, max_user_id=42)


class TestUnsubscribe:
    @pytest.mark.asyncio
    async def test_blocked_user_unsubscribe_idempotent(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        user = SimpleNamespace(is_blocked=True)
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.menu.broadcasts_service.set_subscription",
                   AsyncMock()):
            await menu.do_unsubscribe(event, max_user_id=42)
        from aemr_bot import texts
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert text == texts.UNSUBSCRIBE_CONFIRMED

    @pytest.mark.asyncio
    async def test_not_subscribed_says_already(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        user = SimpleNamespace(is_blocked=False)
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.menu.broadcasts_service.is_subscribed",
                   AsyncMock(return_value=False)):
            await menu.do_unsubscribe(event, max_user_id=42)
        from aemr_bot import texts
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert text == texts.UNSUBSCRIBE_ALREADY_OFF

    @pytest.mark.asyncio
    async def test_normal_unsubscribe(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        user = SimpleNamespace(is_blocked=False)
        set_sub = AsyncMock()
        notify = AsyncMock()
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.menu.broadcasts_service.is_subscribed",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.menu.broadcasts_service.set_subscription",
                   set_sub), \
             patch("aemr_bot.handlers.menu.admin_events.notify_broadcast_unsubscribed",
                   notify):
            await menu.do_unsubscribe(event, max_user_id=42)
        set_sub.assert_called_once()
        notify.assert_called_once_with(event.bot, max_user_id=42, source="меню")


class TestBroadcastUnsubscribe:
    @pytest.mark.asyncio
    async def test_already_off_idempotent(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.broadcasts_service.is_subscribed",
                   AsyncMock(return_value=False)):
            await menu.handle_broadcast_unsubscribe(event, max_user_id=42)
        from aemr_bot import texts
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert text == texts.UNSUBSCRIBE_ALREADY_OFF

    @pytest.mark.asyncio
    async def test_unsubscribe_from_broadcast_notifies_admin(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        set_sub = AsyncMock()
        notify = AsyncMock()
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.broadcasts_service.is_subscribed",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.menu.broadcasts_service.set_subscription",
                   set_sub), \
             patch("aemr_bot.handlers.menu.admin_events.notify_broadcast_unsubscribed",
                   notify):
            await menu.handle_broadcast_unsubscribe(event, max_user_id=42)

        set_sub.assert_called_once()
        notify.assert_called_once_with(
            event.bot,
            max_user_id=42,
            source="кнопка под рассылкой",
        )

    @pytest.mark.asyncio
    async def test_unsubscribe_from_broadcast_button_edits_message(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_callback_event()
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.broadcasts_service.is_subscribed",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.menu.broadcasts_service.set_subscription",
                   AsyncMock()), \
             patch("aemr_bot.handlers.menu.admin_events.notify_broadcast_unsubscribed",
                   AsyncMock()):
            await menu.handle_broadcast_unsubscribe(event, max_user_id=42)

        event.bot.edit_message.assert_called_once()
        assert event.bot.edit_message.call_args.kwargs["message_id"] == "m-1"
        event.bot.send_message.assert_not_called()


class TestConsentAndEraseNotifications:
    @pytest.mark.asyncio
    async def test_consent_revoke_notifies_even_without_open_appeals(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        user = SimpleNamespace(id=1)
        notify = AsyncMock()
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.services.appeals.list_unanswered",
                   AsyncMock(return_value=[])), \
             patch("aemr_bot.handlers.menu.users_service.revoke_consent",
                   AsyncMock()), \
             patch("aemr_bot.services.operators.write_audit", AsyncMock()), \
             patch("aemr_bot.handlers.menu.admin_events.notify_consent_revoked",
                   notify):
            await menu.do_consent_revoke(event, max_user_id=42)

        notify.assert_called_once_with(event.bot, max_user_id=42, open_appeal_ids=[])

    @pytest.mark.asyncio
    async def test_consent_revoke_reposts_open_cards_for_final_reply(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        user = SimpleNamespace(
            id=1,
            first_name="Анна",
            phone="+79991234567",
            is_blocked=False,
        )
        appeal = SimpleNamespace(
            id=9,
            user_id=1,
            locality="Елизовское ГП",
            address="Ленина, 1",
            topic="Дороги",
            summary="Яма",
            attachments=[],
            status="new",
        )
        notify = AsyncMock()
        repost = AsyncMock()
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.services.appeals.list_unanswered",
                   AsyncMock(return_value=[appeal])), \
             patch("aemr_bot.handlers.menu.users_service.revoke_consent",
                   AsyncMock()), \
             patch("aemr_bot.services.operators.write_audit", AsyncMock()), \
             patch("aemr_bot.handlers.menu.admin_events.notify_consent_revoked",
                   notify), \
             patch("aemr_bot.handlers.appeal_runtime.send_to_admin_card",
                   repost):
            await menu.do_consent_revoke(event, max_user_id=42)

        notify.assert_called_once_with(event.bot, max_user_id=42, open_appeal_ids=[9])
        repost.assert_called_once()
        assert repost.call_args.kwargs["appeal_id"] == 9

    @pytest.mark.asyncio
    async def test_forget_notifies_admin_about_deleted_data(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        user = SimpleNamespace(id=1)
        appeal = SimpleNamespace(id=9, user_id=1)
        notify = AsyncMock()
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.services.appeals.list_unanswered",
                   AsyncMock(return_value=[appeal])), \
             patch("aemr_bot.handlers.menu.users_service.erase_pdn", AsyncMock()), \
             patch("aemr_bot.services.operators.write_audit", AsyncMock()), \
             patch("aemr_bot.handlers.menu.admin_events.notify_data_erased",
                   notify):
            await menu.do_forget(event, max_user_id=42)

        notify.assert_called_once_with(event.bot, max_user_id=42, closed_appeal_ids=[9])


class TestSimpleScreens:
    """Smoke-тесты простых экранов: сообщение должно уйти без exception."""

    @pytest.mark.asyncio
    async def test_open_useful_info(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        with patch("aemr_bot.handlers.menu.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.menu.settings_store.get",
                   AsyncMock(return_value="https://example")), \
             patch("aemr_bot.handlers.menu.broadcasts_service.is_subscribed",
                   AsyncMock(return_value=False)):
            await menu.open_useful_info(event)
        event.bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_open_settings(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        await menu.open_settings(event)
        event.bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_open_help(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        await menu.open_help(event)
        event.bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_open_rules(self) -> None:
        from aemr_bot import texts
        from aemr_bot.handlers import menu

        event = _make_event()
        await menu.open_rules(event)

        event.bot.send_message.assert_called_once()
        assert event.bot.send_message.call_args.kwargs.get("text") == texts.RULES_TEXT

    @pytest.mark.asyncio
    async def test_settings_rules_callback_opens_rules(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        with patch("aemr_bot.handlers.menu.ack_callback", AsyncMock()) as ack:
            handled = await menu.handle_callback(
                event,
                payload="settings:rules",
                max_user_id=42,
            )

        assert handled is True
        ack.assert_called_once()
        event.bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_callback_screen_edits_current_card(self) -> None:
        from aemr_bot import texts
        from aemr_bot.handlers import menu

        event = _make_callback_event()
        await menu.open_settings(event)

        event.bot.edit_message.assert_called_once()
        kwargs = event.bot.edit_message.call_args.kwargs
        assert kwargs["message_id"] == "m-1"
        assert kwargs["text"] == texts.SETTINGS_MENU_TITLE
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_callback_screen_falls_back_to_new_message_when_edit_fails(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_callback_event()
        event.bot.edit_message.side_effect = RuntimeError("MAX edit failed")

        await menu.open_settings(event)

        event.bot.edit_message.assert_called_once()
        event.bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_open_goodbye(self) -> None:
        from aemr_bot.handlers import menu

        event = _make_event()
        await menu.open_goodbye(event)
        event.bot.send_message.assert_called_once()
