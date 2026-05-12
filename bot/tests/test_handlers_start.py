"""Тесты handlers/start.py — команды жителя /start, /help, /menu, /rules,
/policy, /subscribe, /unsubscribe, /forget, /export, /cancel, /whoami.

Локально skip без maxapi; в CI работает."""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("maxapi", reason="handlers тесты требуют maxapi")


def _make_event(*, chat_id: int = 100, user_id: int = 42, first_name: str = "Иван") -> SimpleNamespace:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return SimpleNamespace(
        bot=bot,
        message=SimpleNamespace(
            answer=AsyncMock(),
            sender=SimpleNamespace(user_id=user_id, first_name=first_name),
            recipient=SimpleNamespace(chat_id=chat_id),
            body=SimpleNamespace(text="", attachments=[], mid="m-1"),
        ),
        user=SimpleNamespace(user_id=user_id, first_name=first_name),
    )


@asynccontextmanager
async def _fake_session_scope():
    yield MagicMock()


class TestEnsureUser:
    """_ensure_user — должен звать get_or_create."""

    @pytest.mark.asyncio
    async def test_creates_user_with_first_name(self) -> None:
        from aemr_bot.handlers import start

        event = _make_event(first_name="Анна")
        get_or_create = AsyncMock(return_value=SimpleNamespace(id=1))
        with patch("aemr_bot.handlers.start.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.start.users_service.get_or_create",
                   get_or_create):
            result = await start._ensure_user(event)
        get_or_create.assert_called_once()
        assert result.id == 1

    @pytest.mark.asyncio
    async def test_returns_none_without_user_id(self) -> None:
        from aemr_bot.handlers import start

        event = SimpleNamespace(message=None, user=None)
        result = await start._ensure_user(event)
        assert result is None


class TestBuildMainMenu:
    @pytest.mark.asyncio
    async def test_subscribed_state_reflected(self) -> None:
        from aemr_bot.handlers import start

        with patch("aemr_bot.handlers.start.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.start.broadcasts_service.is_subscribed",
                   AsyncMock(return_value=True)):
            kb = await start._build_main_menu(max_user_id=42)
        assert kb is not None

    @pytest.mark.asyncio
    async def test_no_user_id_uses_default(self) -> None:
        from aemr_bot.handlers import start

        kb = await start._build_main_menu(max_user_id=None)
        assert kb is not None


class TestResetFunnelIfStuck:
    @pytest.mark.asyncio
    async def test_resets_when_user_in_pending_state(self) -> None:
        from aemr_bot.db.models import DialogState
        from aemr_bot.handlers import start

        user = SimpleNamespace(dialog_state=DialogState.AWAITING_NAME.value)
        reset = AsyncMock()
        with patch("aemr_bot.handlers.start.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.start.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.start.users_service.reset_state", reset):
            await start._reset_funnel_if_stuck(42)
        reset.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_op_in_idle_state(self) -> None:
        from aemr_bot.db.models import DialogState
        from aemr_bot.handlers import start

        user = SimpleNamespace(dialog_state=DialogState.IDLE.value)
        reset = AsyncMock()
        with patch("aemr_bot.handlers.start.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.start.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.start.users_service.reset_state", reset):
            await start._reset_funnel_if_stuck(42)
        reset.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_no_user_id(self) -> None:
        from aemr_bot.handlers import start

        # Не должно лезть в БД.
        await start._reset_funnel_if_stuck(None)


class TestCmdStart:
    @pytest.mark.asyncio
    async def test_cmd_start_responds_with_welcome(self) -> None:
        from aemr_bot.handlers import start

        event = _make_event()
        user = SimpleNamespace(id=1, dialog_state="idle")
        with patch("aemr_bot.handlers.start.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.start.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.start.users_service.reset_state",
                   AsyncMock()), \
             patch("aemr_bot.handlers.start.broadcasts_service.is_subscribed",
                   AsyncMock(return_value=False)):
            await start.cmd_start(event)
        event.bot.send_message.assert_called_once()


class TestCmdHelp:
    @pytest.mark.asyncio
    async def test_responds_with_help_text(self) -> None:
        from aemr_bot.handlers import start

        event = _make_event()
        with patch("aemr_bot.handlers.start.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.start.broadcasts_service.is_subscribed",
                   AsyncMock(return_value=False)):
            await start.cmd_help(event)
        event.bot.send_message.assert_called_once()
        from aemr_bot import texts
        assert event.bot.send_message.call_args.kwargs.get("text") == texts.HELP_USER


class TestCmdRules:
    @pytest.mark.asyncio
    async def test_responds_with_rules_text(self) -> None:
        from aemr_bot import texts
        from aemr_bot.handlers import start

        event = _make_event()
        await start.cmd_rules(event)

        event.bot.send_message.assert_called_once()
        assert event.bot.send_message.call_args.kwargs.get("text") == texts.RULES_TEXT


class TestCmdMenu:
    @pytest.mark.asyncio
    async def test_responds_with_welcome(self) -> None:
        from aemr_bot.db.models import DialogState
        from aemr_bot.handlers import start

        event = _make_event()
        user = SimpleNamespace(dialog_state=DialogState.IDLE.value)
        with patch("aemr_bot.handlers.start.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.start.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.start.broadcasts_service.is_subscribed",
                   AsyncMock(return_value=False)):
            await start.cmd_menu(event)
        event.bot.send_message.assert_called_once()


class TestCmdPolicy:
    @pytest.mark.asyncio
    async def test_uses_cached_token_to_send_pdf(self) -> None:
        from aemr_bot.handlers import start

        event = _make_event()
        with patch("aemr_bot.handlers.start.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.start.settings_store.get",
                   AsyncMock(side_effect=["TOK-X", None])), \
             patch("aemr_bot.handlers.start.policy_service.build_file_attachment",
                   return_value={"type": "file"}):
            await start.cmd_policy(event)
        # PDF c токеном отправлен.
        event.bot.send_message.assert_called_once()
        kwargs = event.bot.send_message.call_args.kwargs
        assert kwargs.get("attachments") is not None

    @pytest.mark.asyncio
    async def test_falls_back_to_url_when_no_token(self) -> None:
        from aemr_bot.handlers import start

        event = _make_event()
        # ensure_uploaded возвращает None — нет токена. Должно отправить
        # fallback с url.
        with patch("aemr_bot.handlers.start.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.start.settings_store.get",
                   AsyncMock(side_effect=[None, "https://policy.example/p.pdf"])), \
             patch("aemr_bot.handlers.start.policy_service.ensure_uploaded",
                   AsyncMock(return_value=None)):
            await start.cmd_policy(event)
        # Должен быть отправлен fallback с URL.
        assert event.bot.send_message.called or event.message.answer.called

    @pytest.mark.asyncio
    async def test_no_chat_id_returns_silently(self) -> None:
        from aemr_bot.handlers import start

        # Событие без recipient.chat_id и без get_ids
        event = SimpleNamespace(
            bot=MagicMock(),
            message=None,
            user=None,
        )
        await start.cmd_policy(event)
        # Тихо вышло.


class TestCmdSubscribeUnsubscribe:
    @pytest.mark.asyncio
    async def test_subscribe_delegates_to_menu(self) -> None:
        from aemr_bot.handlers import start

        event = _make_event()
        with patch("aemr_bot.handlers.menu.do_subscribe", AsyncMock()) as do_sub:
            await start.cmd_subscribe(event)
        do_sub.assert_called_once()

    @pytest.mark.asyncio
    async def test_unsubscribe_delegates_to_menu(self) -> None:
        from aemr_bot.handlers import start

        event = _make_event()
        with patch("aemr_bot.handlers.menu.do_unsubscribe", AsyncMock()) as do_unsub:
            await start.cmd_unsubscribe(event)
        do_unsub.assert_called_once()

    @pytest.mark.asyncio
    async def test_subscribe_no_user_id_skips(self) -> None:
        from aemr_bot.handlers import start

        event = SimpleNamespace(message=None, user=None)
        with patch("aemr_bot.handlers.menu.do_subscribe", AsyncMock()) as do_sub:
            await start.cmd_subscribe(event)
        do_sub.assert_not_called()


class TestCmdForget:
    @pytest.mark.asyncio
    async def test_writes_audit_and_erases(self) -> None:
        from aemr_bot.handlers import start

        event = _make_event()
        write_audit = AsyncMock()
        erase = AsyncMock()
        with patch("aemr_bot.handlers.start.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.start.ops_service.write_audit", write_audit), \
             patch("aemr_bot.handlers.start.users_service.erase_pdn", erase):
            await start.cmd_forget(event)
        write_audit.assert_called_once()
        erase.assert_called_once()


class TestCmdCancel:
    @pytest.mark.asyncio
    async def test_resets_state_and_opens_menu(self) -> None:
        from aemr_bot.handlers import start

        event = _make_event()
        reset = AsyncMock()
        with patch("aemr_bot.handlers.start.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.start.users_service.reset_state", reset), \
             patch("aemr_bot.handlers.menu.open_main_menu", AsyncMock()) as open_menu:
            await start.cmd_cancel(event)
        reset.assert_called_once()
        open_menu.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_user_id_skips(self) -> None:
        from aemr_bot.handlers import start

        event = SimpleNamespace(message=None, user=None)
        # Не должно бросить.
        await start.cmd_cancel(event)


class TestCmdExport:
    @pytest.mark.asyncio
    async def test_returns_json_with_user_data(self) -> None:
        from aemr_bot.handlers import start

        event = _make_event()
        user = SimpleNamespace(
            id=1,
            max_user_id=42,
            first_name="Иван",
            phone="79001234567",
            consent_pdn_at=None,
            consent_revoked_at=None,
            consent_broadcast_at=None,
            subscribed_broadcast=False,
        )
        with patch("aemr_bot.handlers.start.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.start.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.start.appeals_service.list_for_user",
                   AsyncMock(return_value=[])):
            await start.cmd_export(event)
        # Был ответ с json'ом.
        assert event.bot.send_message.called or event.message.answer.called
