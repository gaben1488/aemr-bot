"""Тесты для handlers/admin_panel — общие операции админ-панели
(выделено из admin_commands.py рефакторингом 2026-05-10).

Покрываем:
- parse_arg / get_text — pure
- show_op_menu: с/без operator, with pin (extract_message_id, pin_message)
- run_open_tickets / run_diag / run_backup: auth-гейты
- _do_open_tickets: пустой / непустой список
- _do_backup: success / db_backup exception / db_backup returns None
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests._helpers import fake_session_scope as _fake_session_scope
from tests._helpers import make_event


def _make_event(*, user_id: int = 7) -> SimpleNamespace:
    # Обёртка над tests/_helpers.make_event. admin_panel-handler'ы
    # умеют закреплять сообщения — доставляем bot.pin_message,
    # которого нет в базовой фабрике.
    event = make_event(chat_id=555, user_id=user_id)
    event.bot.pin_message = AsyncMock()
    return event


# --- pure helpers -------------------------------------------------------------


class TestParseArg:
    def test_no_arg(self) -> None:
        from aemr_bot.handlers.admin_panel import parse_arg

        assert parse_arg("/cmd") == ""

    def test_single_arg(self) -> None:
        from aemr_bot.handlers.admin_panel import parse_arg

        assert parse_arg("/cmd today") == "today"

    def test_arg_with_extra_whitespace(self) -> None:
        from aemr_bot.handlers.admin_panel import parse_arg

        assert parse_arg("/cmd   max_user_id=42   ") == "max_user_id=42"

    def test_multi_word_arg(self) -> None:
        from aemr_bot.handlers.admin_panel import parse_arg

        assert parse_arg("/setting topics [1, 2]") == "topics [1, 2]"


# --- show_op_menu -------------------------------------------------------------


class TestShowOpMenu:
    @pytest.mark.asyncio
    async def test_no_operator_uses_default_flags(self) -> None:
        pytest.importorskip("maxapi")
        from aemr_bot.handlers import admin_panel

        event = _make_event()
        with patch("aemr_bot.handlers.admin_panel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_panel.get_operator",
                   AsyncMock(return_value=None)), \
             patch("aemr_bot.services.appeals.count_open",
                   AsyncMock(return_value=3)):
            await admin_panel.show_op_menu(event, pin=False)
        event.bot.send_message.assert_called_once()
        event.bot.pin_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_it_operator_can_pin(self) -> None:
        pytest.importorskip("maxapi")
        from aemr_bot.db.models import OperatorRole
        from aemr_bot.handlers import admin_panel

        event = _make_event()
        # send_message return — SendedMessage-like для extract_message_id
        event.bot.send_message.return_value = SimpleNamespace(
            message=SimpleNamespace(body=SimpleNamespace(mid="m-pin"))
        )
        op = SimpleNamespace(role=OperatorRole.IT.value)
        with patch("aemr_bot.handlers.admin_panel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_panel.get_operator",
                   AsyncMock(return_value=op)), \
             patch("aemr_bot.services.appeals.count_open",
                   AsyncMock(return_value=2)):
            await admin_panel.show_op_menu(event, pin=True)
        event.bot.pin_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_count_open_failure_does_not_crash(self) -> None:
        """Если count_open упал, меню всё равно показываем с None-счётчиком."""
        pytest.importorskip("maxapi")
        from aemr_bot.handlers import admin_panel

        event = _make_event()
        with patch("aemr_bot.handlers.admin_panel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_panel.get_operator",
                   AsyncMock(return_value=None)), \
             patch("aemr_bot.services.appeals.count_open",
                   AsyncMock(side_effect=RuntimeError("db down"))):
            await admin_panel.show_op_menu(event, pin=False)
        event.bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_pin_failure_does_not_crash(self) -> None:
        pytest.importorskip("maxapi")
        from aemr_bot.handlers import admin_panel

        event = _make_event()
        event.bot.send_message.return_value = SimpleNamespace(
            message=SimpleNamespace(body=SimpleNamespace(mid="m-pin"))
        )
        event.bot.pin_message.side_effect = RuntimeError("pin failed")
        with patch("aemr_bot.handlers.admin_panel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_panel.get_operator",
                   AsyncMock(return_value=None)), \
             patch("aemr_bot.services.appeals.count_open",
                   AsyncMock(return_value=0)):
            # Не должно бросить
            await admin_panel.show_op_menu(event, pin=True)


# --- run_open_tickets / run_diag / run_backup auth gates ---------------------


class TestRunOpenTickets:
    @pytest.mark.asyncio
    async def test_not_operator_returns(self) -> None:
        pytest.importorskip("maxapi")
        from aemr_bot.handlers import admin_panel

        event = _make_event()
        with patch("aemr_bot.handlers.admin_panel.ensure_operator",
                   AsyncMock(return_value=False)):
            await admin_panel.run_open_tickets(event)
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_operator_calls_do_open_tickets(self) -> None:
        pytest.importorskip("maxapi")
        from aemr_bot.handlers import admin_panel

        event = _make_event()
        do_open = AsyncMock()
        with patch("aemr_bot.handlers.admin_panel.ensure_operator",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_panel._do_open_tickets", do_open):
            await admin_panel.run_open_tickets(event)
        do_open.assert_called_once_with(event)


class TestRunDiag:
    @pytest.mark.asyncio
    async def test_not_operator_returns(self) -> None:
        pytest.importorskip("maxapi")
        from aemr_bot.handlers import admin_panel

        event = _make_event()
        with patch("aemr_bot.handlers.admin_panel.ensure_operator",
                   AsyncMock(return_value=False)):
            await admin_panel.run_diag(event)
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_operator_calls_do_diag(self) -> None:
        pytest.importorskip("maxapi")
        from aemr_bot.handlers import admin_panel

        event = _make_event()
        do_diag = AsyncMock()
        with patch("aemr_bot.handlers.admin_panel.ensure_operator",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_panel._do_diag", do_diag):
            await admin_panel.run_diag(event)
        do_diag.assert_called_once()


class TestRunBackup:
    @pytest.mark.asyncio
    async def test_not_it_returns(self) -> None:
        pytest.importorskip("maxapi")
        from aemr_bot.handlers import admin_panel

        event = _make_event()
        with patch("aemr_bot.handlers._auth.ensure_role",
                   AsyncMock(return_value=False)):
            await admin_panel.run_backup(event)
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_it_calls_do_backup(self) -> None:
        pytest.importorskip("maxapi")
        from aemr_bot.handlers import admin_panel

        event = _make_event()
        do_backup = AsyncMock()
        with patch("aemr_bot.handlers._auth.ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_panel._do_backup", do_backup):
            await admin_panel.run_backup(event)
        do_backup.assert_called_once()


# --- _do_backup ---------------------------------------------------------------


class TestDoBackup:
    @pytest.mark.asyncio
    async def test_success_path(self, tmp_path) -> None:
        pytest.importorskip("maxapi")
        from aemr_bot.handlers import admin_panel

        event = _make_event()
        # Мокаем pg_dump → реальный путь к файлу для st_size
        backup_file = tmp_path / "backup.sql"
        backup_file.write_bytes(b"x" * 2048)  # 2 КБ

        with patch("aemr_bot.services.db_backup.backup_db",
                   AsyncMock(return_value=backup_file)):
            await admin_panel._do_backup(event)
        # Два сообщения: «запускаю…» и «✅ готов».
        assert event.bot.send_message.call_count == 2
        last_text = event.bot.send_message.call_args_list[-1].kwargs["text"]
        assert "✅" in last_text
        assert "backup.sql" in last_text

    @pytest.mark.asyncio
    async def test_returns_none_path(self) -> None:
        pytest.importorskip("maxapi")
        from aemr_bot.handlers import admin_panel

        event = _make_event()
        with patch("aemr_bot.services.db_backup.backup_db",
                   AsyncMock(return_value=None)):
            await admin_panel._do_backup(event)
        last_text = event.bot.send_message.call_args_list[-1].kwargs["text"]
        assert "не выполнен" in last_text.lower()

    @pytest.mark.asyncio
    async def test_exception_path(self) -> None:
        pytest.importorskip("maxapi")
        from aemr_bot.handlers import admin_panel

        event = _make_event()
        with patch("aemr_bot.services.db_backup.backup_db",
                   AsyncMock(side_effect=RuntimeError("disk full"))):
            await admin_panel._do_backup(event)
        last_text = event.bot.send_message.call_args_list[-1].kwargs["text"]
        assert "упал" in last_text.lower()
        assert "disk full" in last_text


# --- _do_open_tickets ---------------------------------------------------------


class TestDoOpenTickets:
    @pytest.mark.asyncio
    async def test_empty_list_friendly_message(self) -> None:
        pytest.importorskip("maxapi")
        from aemr_bot.handlers import admin_panel

        event = _make_event()
        # Mockаем session.scalars(query) — возвращает asyncio-aware Mock
        # с .all() = [].
        scalars_result = MagicMock()
        scalars_result.all = MagicMock(return_value=[])
        session = MagicMock()
        session.scalars = AsyncMock(return_value=scalars_result)

        @asynccontextmanager
        async def fake_scope():
            yield session

        with patch("aemr_bot.handlers.admin_panel.session_scope", fake_scope):
            await admin_panel._do_open_tickets(event)
        event.bot.send_message.assert_called_once()
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "🎉" in text
