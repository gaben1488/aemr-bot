"""Тесты для трёх небольших admin-handlers (выделены из admin_commands
рефакторингом 2026-05-10):

- handlers/admin_audience.py — меню «Аудитория и согласия» + точечные
  block/unblock/erase
- handlers/admin_settings.py — меню «Настройки бота» + show value
- handlers/admin_stats.py — XLSX-выгрузка по периоду

Локально skip без maxapi; в CI работает (без БД — все services мокаются).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("maxapi", reason="handlers тесты требуют maxapi")


def _make_event(*, chat_id: int = 555, user_id: int = 7) -> SimpleNamespace:
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


@asynccontextmanager
async def _fake_session_scope():
    yield MagicMock()


# --- _mask_phone (PII protection in admin lists) -----------------------------


class TestMaskPhone:
    def test_none_returns_dash(self) -> None:
        from aemr_bot.handlers.admin_audience import _mask_phone

        assert _mask_phone(None) == "—"

    def test_empty_returns_dash(self) -> None:
        from aemr_bot.handlers.admin_audience import _mask_phone

        assert _mask_phone("") == "—"

    def test_full_ru_phone_masked(self) -> None:
        from aemr_bot.handlers.admin_audience import _mask_phone

        # 11-значные RU номера показываются как +7***LAST4
        assert _mask_phone("+79991234567") == "+7***4567"
        assert _mask_phone("89991234567") == "+7***4567"
        assert _mask_phone("79991234567") == "+7***4567"

    def test_short_garbage_kept_as_is(self) -> None:
        """Если телефон короче 4 цифр — это явно мусор; маскировать
        там нечего. Пусть оператор увидит, что номер сломан."""
        from aemr_bot.handlers.admin_audience import _mask_phone

        assert _mask_phone("ab") == "ab"
        assert _mask_phone("123") == "123"

    def test_non_ru_format(self) -> None:
        from aemr_bot.handlers.admin_audience import _mask_phone

        # Без российского префикса (7/8 в начале и >=11 цифр) — generic +***LAST4
        assert _mask_phone("+1234567") == "+***4567"


# --- admin_audience -----------------------------------------------------------


class TestAudienceMenu:
    @pytest.mark.asyncio
    async def test_run_audience_menu_blocked_for_non_it(self) -> None:
        from aemr_bot.handlers import admin_audience

        event = _make_event()
        with patch(
            "aemr_bot.handlers.admin_audience.ensure_role",
            AsyncMock(return_value=False),
        ):
            await admin_audience.run_audience_menu(event)
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_audience_menu_sends_for_it(self) -> None:
        from aemr_bot.handlers import admin_audience

        event = _make_event()
        with patch(
            "aemr_bot.handlers.admin_audience.ensure_role",
            AsyncMock(return_value=True),
        ):
            await admin_audience.run_audience_menu(event)
        event.bot.send_message.assert_called_once()
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "Аудитория" in text


class TestAudienceAction:
    @pytest.mark.asyncio
    async def test_block_action_sets_blocked_and_audits(self) -> None:
        from aemr_bot.handlers import admin_audience

        event = _make_event()
        set_blocked = AsyncMock(return_value=True)
        write_audit = AsyncMock()
        with patch("aemr_bot.handlers.admin_audience.ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_audience.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_audience.users_service.set_blocked",
                   set_blocked), \
             patch("aemr_bot.handlers.admin_audience.operators_service.write_audit",
                   write_audit), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_audience.run_audience_action(event, "op:aud:block:42")
        set_blocked.assert_called_once()
        assert set_blocked.call_args.kwargs == {"blocked": True}
        write_audit.assert_called_once()
        event.bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_unblock_action(self) -> None:
        from aemr_bot.handlers import admin_audience

        event = _make_event()
        set_blocked = AsyncMock(return_value=True)
        with patch("aemr_bot.handlers.admin_audience.ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_audience.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_audience.users_service.set_blocked",
                   set_blocked), \
             patch("aemr_bot.handlers.admin_audience.operators_service.write_audit",
                   AsyncMock()), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_audience.run_audience_action(event, "op:aud:unblock:42")
        assert set_blocked.call_args.kwargs == {"blocked": False}

    @pytest.mark.asyncio
    async def test_erase_action(self) -> None:
        from aemr_bot.handlers import admin_audience

        event = _make_event()
        erase = AsyncMock(return_value=True)
        with patch("aemr_bot.handlers.admin_audience.ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_audience.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_audience.users_service.erase_pdn",
                   erase), \
             patch("aemr_bot.handlers.admin_audience.operators_service.write_audit",
                   AsyncMock()), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_audience.run_audience_action(event, "op:aud:erase:42")
        erase.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_target_id_returns_silently(self) -> None:
        from aemr_bot.handlers import admin_audience

        event = _make_event()
        with patch("aemr_bot.handlers.admin_audience.ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_audience.run_audience_action(
                event, "op:aud:block:notanint"
            )
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_subs_list_with_users(self) -> None:
        from aemr_bot.handlers import admin_audience

        event = _make_event()
        users = [
            SimpleNamespace(max_user_id=1, first_name="Иван", phone="+79001",
                            is_blocked=False),
            SimpleNamespace(max_user_id=2, first_name=None, phone=None,
                            is_blocked=False),
        ]
        with patch("aemr_bot.handlers.admin_audience.ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_audience.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_audience.users_service.list_subscribers",
                   AsyncMock(return_value=users)), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_audience.run_audience_action(event, "op:aud:subs")
        # 1 заголовок + 2 строки = 3 send_message
        assert event.bot.send_message.call_count == 3

    @pytest.mark.asyncio
    async def test_consent_list_empty(self) -> None:
        from aemr_bot.handlers import admin_audience

        event = _make_event()
        with patch("aemr_bot.handlers.admin_audience.ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_audience.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_audience.users_service.list_consented",
                   AsyncMock(return_value=[])), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_audience.run_audience_action(event, "op:aud:consent")
        # Один send «список пуст»
        event.bot.send_message.assert_called_once()
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "пуст" in text

    @pytest.mark.asyncio
    async def test_blocked_list(self) -> None:
        from aemr_bot.handlers import admin_audience

        event = _make_event()
        users = [SimpleNamespace(max_user_id=1, first_name="X", phone="—",
                                 is_blocked=True)]
        with patch("aemr_bot.handlers.admin_audience.ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_audience.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_audience.users_service.list_blocked",
                   AsyncMock(return_value=users)), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_audience.run_audience_action(event, "op:aud:blocked")
        # header + 1 user-line
        assert event.bot.send_message.call_count == 2

    @pytest.mark.asyncio
    async def test_unknown_suffix_returns(self) -> None:
        from aemr_bot.handlers import admin_audience

        event = _make_event()
        with patch("aemr_bot.handlers.admin_audience.ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_audience.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_audience.run_audience_action(event, "op:aud:unknown")
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_block_without_audit_when_set_blocked_returns_false(self) -> None:
        """Если жителя нет (set_blocked → False), audit не пишем,
        но шлём «Не удалось»."""
        from aemr_bot.handlers import admin_audience

        event = _make_event()
        set_blocked = AsyncMock(return_value=False)
        write_audit = AsyncMock()
        with patch("aemr_bot.handlers.admin_audience.ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_audience.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_audience.users_service.set_blocked",
                   set_blocked), \
             patch("aemr_bot.handlers.admin_audience.operators_service.write_audit",
                   write_audit), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_audience.run_audience_action(event, "op:aud:block:99")
        write_audit.assert_not_called()
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "Не удалось" in text


# --- admin_settings -----------------------------------------------------------


class TestSettingsMenu:
    @pytest.mark.asyncio
    async def test_blocked_for_non_it(self) -> None:
        from aemr_bot.handlers import admin_settings

        event = _make_event()
        with patch("aemr_bot.handlers.admin_settings.ensure_role",
                   AsyncMock(return_value=False)):
            await admin_settings.run_settings_menu(event)
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_lists_keys(self) -> None:
        from aemr_bot.handlers import admin_settings

        event = _make_event()
        with patch("aemr_bot.handlers.admin_settings.ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_settings.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_settings.settings_store.list_keys",
                   AsyncMock(return_value=["topics", "localities"])):
            await admin_settings.run_settings_menu(event)
        event.bot.send_message.assert_called_once()


class TestSettingsAction:
    @pytest.mark.asyncio
    async def test_blocked_for_non_it(self) -> None:
        from aemr_bot.handlers import admin_settings

        event = _make_event()
        with patch("aemr_bot.handlers.admin_settings.ensure_role",
                   AsyncMock(return_value=False)):
            await admin_settings.run_settings_action(event, "op:setkey:topics")
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_key_returns_silently(self) -> None:
        from aemr_bot.handlers import admin_settings

        event = _make_event()
        with patch("aemr_bot.handlers.admin_settings.ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_settings.run_settings_action(event, "op:setkey:")
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_shows_current_value_and_command_template(self) -> None:
        from aemr_bot.handlers import admin_settings

        event = _make_event()
        with patch("aemr_bot.handlers.admin_settings.ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_settings.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_settings.settings_store.get",
                   AsyncMock(return_value=["Дороги", "ЖКХ"])), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_settings.run_settings_action(event, "op:setkey:topics")
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "topics" in text
        assert "/setting topics" in text
        # JSON-рендер списка
        assert "Дороги" in text

    @pytest.mark.asyncio
    async def test_none_value_renders_dash(self) -> None:
        from aemr_bot.handlers import admin_settings

        event = _make_event()
        with patch("aemr_bot.handlers.admin_settings.ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_settings.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_settings.settings_store.get",
                   AsyncMock(return_value=None)), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_settings.run_settings_action(event, "op:setkey:topics")
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "—" in text  # отрисовка пустого значения

    @pytest.mark.asyncio
    async def test_long_value_truncated(self) -> None:
        from aemr_bot.handlers import admin_settings

        event = _make_event()
        big_value = ["x" * 1000, "y" * 1000]  # JSON > 1500 символов
        with patch("aemr_bot.handlers.admin_settings.ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_settings.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_settings.settings_store.get",
                   AsyncMock(return_value=big_value)), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_settings.run_settings_action(event, "op:setkey:topics")
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "обрезано" in text


# --- admin_stats --------------------------------------------------------------


class TestStatsMenu:
    @pytest.mark.asyncio
    async def test_blocked_for_non_operator(self) -> None:
        from aemr_bot.handlers import admin_stats

        event = _make_event()
        with patch("aemr_bot.handlers.admin_stats.ensure_operator",
                   AsyncMock(return_value=False)):
            await admin_stats.run_stats_menu(event)
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_period_choice(self) -> None:
        from aemr_bot.handlers import admin_stats

        event = _make_event()
        with patch("aemr_bot.handlers.admin_stats.ensure_operator",
                   AsyncMock(return_value=True)):
            await admin_stats.run_stats_menu(event)
        event.bot.send_message.assert_called_once()
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "период" in text.lower()


class TestRunStats:
    @pytest.mark.asyncio
    async def test_invalid_period_returns_silently(self) -> None:
        from aemr_bot.handlers import admin_stats

        event = _make_event()
        with patch("aemr_bot.handlers.admin_stats.ensure_operator",
                   AsyncMock(return_value=True)):
            await admin_stats.run_stats(event, "bogus_period")
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_blocked_for_non_operator(self) -> None:
        from aemr_bot.handlers import admin_stats

        event = _make_event()
        with patch("aemr_bot.handlers.admin_stats.ensure_operator",
                   AsyncMock(return_value=False)):
            await admin_stats.run_stats(event, "today")
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_count_sends_empty_message(self) -> None:
        from aemr_bot.handlers import admin_stats

        event = _make_event()
        with patch("aemr_bot.handlers.admin_stats.ensure_operator",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_stats.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_stats.stats_service.build_xlsx",
                   AsyncMock(return_value=(b"", "сегодня", 0))), \
             patch("aemr_bot.handlers.admin_panel.show_op_menu",
                   AsyncMock()):
            await admin_stats.run_stats(event, "today")
        event.bot.send_message.assert_called_once()
        text = event.bot.send_message.call_args.kwargs["text"]
        # texts.OP_STATS_EMPTY
        from aemr_bot import texts
        assert text == texts.OP_STATS_EMPTY

    @pytest.mark.asyncio
    async def test_upload_fail_sends_warning(self) -> None:
        from aemr_bot.handlers import admin_stats

        event = _make_event()
        with patch("aemr_bot.handlers.admin_stats.ensure_operator",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_stats.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_stats.stats_service.build_xlsx",
                   AsyncMock(return_value=(b"data", "сегодня", 5))), \
             patch("aemr_bot.services.uploads.upload_bytes",
                   AsyncMock(return_value=None)), \
             patch("aemr_bot.handlers.admin_panel.show_op_menu",
                   AsyncMock()):
            await admin_stats.run_stats(event, "today")
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "не удалось" in text.lower()

    @pytest.mark.asyncio
    async def test_success_uploads_and_attaches(self) -> None:
        from aemr_bot.handlers import admin_stats

        event = _make_event()
        with patch("aemr_bot.handlers.admin_stats.ensure_operator",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_stats.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_stats.stats_service.build_xlsx",
                   AsyncMock(return_value=(b"data", "сегодня", 5))), \
             patch("aemr_bot.services.uploads.upload_bytes",
                   AsyncMock(return_value="upload-token")), \
             patch("aemr_bot.services.uploads.file_attachment",
                   return_value={"type": "file"}), \
             patch("aemr_bot.handlers.admin_panel.show_op_menu",
                   AsyncMock()):
            await admin_stats.run_stats(event, "today")
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "Статистика" in text
        assert "5 обращений" in text


class TestRunStatsToday:
    @pytest.mark.asyncio
    async def test_runs_today_period(self) -> None:
        from aemr_bot.handlers import admin_stats

        event = _make_event()
        with patch("aemr_bot.handlers.admin_stats.ensure_operator",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_stats.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_stats.stats_service.build_xlsx",
                   AsyncMock(return_value=(b"", "сегодня", 0))):
            await admin_stats.run_stats_today(event)
        event.bot.send_message.assert_called_once()
