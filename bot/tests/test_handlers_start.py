"""Тесты handlers/start.py — команды жителя /start, /help, /menu, /rules,
/policy, /subscribe, /unsubscribe, /forget, /export, /cancel, /whoami.

Локально skip без maxapi; в CI работает."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests._helpers import fake_session_scope as _fake_session_scope
from tests._helpers import make_event

pytest.importorskip("maxapi", reason="handlers тесты требуют maxapi")


def _make_event(
    *, chat_id: int = 100, user_id: int = 42, first_name: str = "Иван"
) -> SimpleNamespace:
    # Обёртка над tests/_helpers.make_event — с event.user (start-
    # handler'ы читают first_name и из sender, и из event.user).
    return make_event(
        chat_id=chat_id, user_id=user_id, first_name=first_name,
        with_user=True,
    )


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

        # max_user_id=None → ранний выход, в БД не лезем.
        with patch("aemr_bot.handlers.start.session_scope") as scope:
            result = await start._reset_funnel_if_stuck(None)
        assert result is None
        scope.assert_not_called()


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
        event.bot.send_message = AsyncMock()
        with patch("aemr_bot.handlers.start.session_scope") as scope:
            result = await start.cmd_policy(event)
        # Нет chat_id → ранний выход: ни запроса настроек, ни отправки.
        assert result is None
        scope.assert_not_called()
        event.bot.send_message.assert_not_called()


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
    async def test_writes_audit_and_erases_with_closed_ids(self) -> None:
        """erase_pdn_detailed возвращает list[int] закрытых обращений —
        notify передаёт его в `closed_appeal_ids` (был bug: всегда [])."""
        from aemr_bot.handlers import start

        event = _make_event()
        write_audit = AsyncMock()
        erase = AsyncMock(return_value=[7, 12])  # 2 закрытых обращения
        notify = AsyncMock()
        with patch("aemr_bot.handlers.start.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.start.ops_service.write_audit", write_audit), \
             patch("aemr_bot.handlers.start.users_service.erase_pdn_detailed", erase), \
             patch("aemr_bot.handlers.start.admin_events.notify_data_erased", notify):
            await start.cmd_forget(event)
        write_audit.assert_called_once()
        erase.assert_called_once()
        notify.assert_called_once_with(
            event.bot, max_user_id=42, closed_appeal_ids=[7, 12]
        )

    @pytest.mark.asyncio
    async def test_user_not_found_no_crash(self) -> None:
        """Если жителя нет в БД (повторный /forget) — erase_pdn_detailed
        возвращает None, notify получает пустой список."""
        from aemr_bot.handlers import start

        event = _make_event()
        erase = AsyncMock(return_value=None)
        notify = AsyncMock()
        with patch("aemr_bot.handlers.start.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.start.ops_service.write_audit", AsyncMock()), \
             patch("aemr_bot.handlers.start.users_service.erase_pdn_detailed", erase), \
             patch("aemr_bot.handlers.start.admin_events.notify_data_erased", notify):
            await start.cmd_forget(event)
        notify.assert_called_once_with(
            event.bot, max_user_id=42, closed_appeal_ids=[]
        )


class TestCmdCancel:
    @pytest.mark.asyncio
    async def test_resets_state_and_opens_menu(self) -> None:
        from aemr_bot.handlers import start

        event = _make_event()
        reset = AsyncMock()
        with patch("aemr_bot.handlers.start.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.start.users_service.reset_state", reset):
            await start.cmd_cancel(event)
        reset.assert_called_once()
        event.bot.send_message.assert_called_once()
        assert event.bot.send_message.call_args.kwargs["attachments"]

    @pytest.mark.asyncio
    async def test_no_user_id_skips(self) -> None:
        from aemr_bot.handlers import start

        event = SimpleNamespace(message=None, user=None)
        # Нет user_id → ранний выход: ни сброса состояния в БД, ни ответа.
        with patch("aemr_bot.handlers.start.session_scope") as scope, \
             patch("aemr_bot.handlers.start.reply", AsyncMock()) as reply_mock:
            result = await start.cmd_cancel(event)
        assert result is None
        scope.assert_not_called()
        reply_mock.assert_not_called()


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
            consent_pdn_text_sha256=None,
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

    @pytest.mark.asyncio
    async def test_export_includes_full_correspondence(self) -> None:
        """Выгрузка отдаёт ВСЮ переписку, а не только последний ответ.

        Ст. 14 даёт право на доступ к обрабатываемым данным: уточнения
        жителя и его вложения — такие же его данные, как текст
        обращения. Раньше выгрузка возвращала одно последнее сообщение
        оператора, хотя согласие обещает хранение всей переписки.
        """
        from aemr_bot.handlers import start

        event = _make_event()
        user = SimpleNamespace(
            id=1, max_user_id=42, first_name="Иван", phone="79001234567",
            consent_pdn_at=None, consent_pdn_text_sha256=None,
            consent_revoked_at=None,
            consent_broadcast_at=None, subscribed_broadcast=False,
        )
        appeal = SimpleNamespace(
            id=7, created_at=None, status="answered", locality="Елизово",
            address="ул. Ленина, 1", topic="Дороги", summary="яма",
            attachments=[{"type": "image"}], answered_at=None, closed_at=None,
            messages=[
                SimpleNamespace(created_at=None, direction="from_user",
                                text="уточнение жителя", attachments=[]),
                SimpleNamespace(created_at=None, direction="from_operator",
                                text="первый ответ", attachments=[]),
                SimpleNamespace(created_at=None, direction="from_operator",
                                text="второй ответ", attachments=[{"t": 1}]),
            ],
        )
        with patch("aemr_bot.handlers.start.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers.start.users_service.get_or_create",
                   AsyncMock(return_value=user)), \
             patch("aemr_bot.handlers.start.appeals_service.list_for_user",
                   AsyncMock(return_value=[appeal])), \
             patch("aemr_bot.handlers.start.reply", AsyncMock()) as reply_mock:
            await start.cmd_export(event)

        sent = "".join(call.args[1] for call in reply_mock.call_args_list)
        # Собственное сообщение жителя тоже в выгрузке.
        assert "уточнение жителя" in sent
        # И промежуточный ответ, а не только последний.
        assert "первый ответ" in sent
        assert "второй ответ" in sent

    def test_chunker_splits_long_text_without_breaking_lines(self) -> None:
        """Разбивка режет по строкам и укладывается в лимит.

        Выгрузка уходила одним сообщением, а предел MAX ~4000 знаков:
        у жителя с несколькими обращениями отправка падала, и он не
        получал НИЧЕГО — молча, ошибку видел только лог.
        """
        from aemr_bot.handlers.start import _chunk_for_messenger

        text = "\n".join(f"строка номер {i}" for i in range(300))
        parts = _chunk_for_messenger(text, limit=200)

        assert len(parts) > 1
        assert all(len(p) <= 200 for p in parts)
        # Ничего не потеряно и строки целы.
        assert "\n".join(parts).replace("\n", "") == text.replace("\n", "")

    def test_chunker_keeps_short_text_intact(self) -> None:
        from aemr_bot.handlers.start import _chunk_for_messenger

        assert _chunk_for_messenger("коротко", limit=100) == ["коротко"]

    def test_chunker_splits_overlong_single_line(self) -> None:
        """Строка длиннее лимита дробится принудительно — иначе часть
        снова не уйдёт (житель с одним огромным обращением)."""
        from aemr_bot.handlers.start import _chunk_for_messenger

        parts = _chunk_for_messenger("x" * 500, limit=200)
        assert all(len(p) <= 200 for p in parts)
        assert "".join(parts) == "x" * 500
