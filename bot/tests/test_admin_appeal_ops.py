"""Тесты для handlers/admin_appeal_ops — действия оператора над
конкретным обращением (выделено из admin_commands рефакторингом
2026-05-10).

Локально skip без maxapi; в CI работает (services мокаются).

Покрываем:
- run_reply_intent: not admin chat, no operator, appeal not found,
  appeal closed, user blocked, happy path
- run_reply_cancel: drop intent, no operator
- run_reopen: not operator, ok, not found
- run_close: not operator, ok, not found
- run_block_for_appeal: not it, appeal not found, ok blocked/unblocked,
  failure
- run_erase_for_appeal: not it, appeal not found, ok, failure
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
        callback=SimpleNamespace(callback_id="cb-1"),
    )


@asynccontextmanager
async def _fake_session_scope():
    yield MagicMock()


# --- run_reply_intent ---------------------------------------------------------


class TestRunReplyIntent:
    @pytest.mark.asyncio
    async def test_not_admin_chat_silently_returns(self) -> None:
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        with patch("aemr_bot.handlers.admin_appeal_ops.is_admin_chat",
                   return_value=False), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_appeal_ops.run_reply_intent(event, 5)
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_operator_returns(self) -> None:
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        with patch("aemr_bot.handlers.admin_appeal_ops.is_admin_chat",
                   return_value=True), \
             patch("aemr_bot.handlers.admin_appeal_ops.ensure_operator",
                   AsyncMock(return_value=False)), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_appeal_ops.run_reply_intent(event, 5)
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_appeal_not_found_message(self) -> None:
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        with patch("aemr_bot.handlers.admin_appeal_ops.is_admin_chat",
                   return_value=True), \
             patch("aemr_bot.handlers.admin_appeal_ops.ensure_operator",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_appeal_ops.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_appeal_ops.appeals_service.get_by_id",
                   AsyncMock(return_value=None)), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_appeal_ops.run_reply_intent(event, 999)
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "999" in text or "не найдено" in text.lower()

    @pytest.mark.asyncio
    async def test_appeal_closed_blocks_reply(self) -> None:
        from aemr_bot.db.models import AppealStatus
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        appeal = SimpleNamespace(
            id=5,
            status=AppealStatus.CLOSED.value,
            user=SimpleNamespace(is_blocked=False),
        )
        with patch("aemr_bot.handlers.admin_appeal_ops.is_admin_chat",
                   return_value=True), \
             patch("aemr_bot.handlers.admin_appeal_ops.ensure_operator",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_appeal_ops.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_appeal_ops.appeals_service.get_by_id",
                   AsyncMock(return_value=appeal)), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_appeal_ops.run_reply_intent(event, 5)
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "закрыто" in text.lower()

    @pytest.mark.asyncio
    async def test_user_blocked_blocks_reply(self) -> None:
        from aemr_bot.db.models import AppealStatus
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        appeal = SimpleNamespace(
            id=5,
            status=AppealStatus.NEW.value,
            user=SimpleNamespace(is_blocked=True),
        )
        with patch("aemr_bot.handlers.admin_appeal_ops.is_admin_chat",
                   return_value=True), \
             patch("aemr_bot.handlers.admin_appeal_ops.ensure_operator",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_appeal_ops.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_appeal_ops.appeals_service.get_by_id",
                   AsyncMock(return_value=appeal)), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_appeal_ops.run_reply_intent(event, 5)
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "заблокирован" in text.lower()

    @pytest.mark.asyncio
    async def test_happy_path_sets_intent(self) -> None:
        from aemr_bot.db.models import AppealStatus
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        appeal = SimpleNamespace(
            id=5,
            status=AppealStatus.NEW.value,
            user=SimpleNamespace(is_blocked=False),
        )
        remember = MagicMock()
        with patch("aemr_bot.handlers.admin_appeal_ops.is_admin_chat",
                   return_value=True), \
             patch("aemr_bot.handlers.admin_appeal_ops.ensure_operator",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_appeal_ops.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_appeal_ops.appeals_service.get_by_id",
                   AsyncMock(return_value=appeal)), \
             patch("aemr_bot.handlers.operator_reply.remember_reply_intent",
                   remember), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_appeal_ops.run_reply_intent(event, 5)
        remember.assert_called_once()
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "ответа" in text.lower()


# --- run_reply_cancel ---------------------------------------------------------


class TestRunReplyCancel:
    @pytest.mark.asyncio
    async def test_drops_intent_and_confirms(self) -> None:
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        with patch("aemr_bot.handlers.operator_reply.drop_reply_intent",
                   return_value=42), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_appeal_ops.run_reply_cancel(event)
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "42" in text and "отменён" in text.lower()

    @pytest.mark.asyncio
    async def test_no_intent_no_confirmation(self) -> None:
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        with patch("aemr_bot.handlers.operator_reply.drop_reply_intent",
                   return_value=None), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_appeal_ops.run_reply_cancel(event)
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "уже закрыт" in text.lower()


# --- run_reopen / run_close ---------------------------------------------------


class TestRunReopen:
    @pytest.mark.asyncio
    async def test_not_operator_returns(self) -> None:
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        with patch("aemr_bot.handlers.admin_appeal_ops.ensure_operator",
                   AsyncMock(return_value=False)):
            await admin_appeal_ops.run_reopen(event, 5)
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_ok_writes_audit(self) -> None:
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        write_audit = AsyncMock()
        with patch("aemr_bot.handlers.admin_appeal_ops.ensure_operator",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_appeal_ops.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_appeal_ops.appeals_service.reopen",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_appeal_ops.operators_service.write_audit",
                   write_audit), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_appeal_ops.run_reopen(event, 5)
        write_audit.assert_called_once()
        event.bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_not_found_no_audit(self) -> None:
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        write_audit = AsyncMock()
        with patch("aemr_bot.handlers.admin_appeal_ops.ensure_operator",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_appeal_ops.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_appeal_ops.appeals_service.reopen",
                   AsyncMock(return_value=False)), \
             patch("aemr_bot.handlers.admin_appeal_ops.operators_service.write_audit",
                   write_audit), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_appeal_ops.run_reopen(event, 999)
        write_audit.assert_not_called()


class TestRunClose:
    @pytest.mark.asyncio
    async def test_not_operator_returns(self) -> None:
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        with patch("aemr_bot.handlers.admin_appeal_ops.ensure_operator",
                   AsyncMock(return_value=False)):
            await admin_appeal_ops.run_close(event, 5)
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_ok_path(self) -> None:
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        with patch("aemr_bot.handlers.admin_appeal_ops.ensure_operator",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_appeal_ops.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_appeal_ops.appeals_service.close",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_appeal_ops.operators_service.write_audit",
                   AsyncMock()), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_appeal_ops.run_close(event, 5)
        event.bot.send_message.assert_called_once()


# --- run_block_for_appeal -----------------------------------------------------


class TestRunBlockForAppeal:
    @pytest.mark.asyncio
    async def test_not_it_returns(self) -> None:
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        with patch("aemr_bot.handlers.admin_appeal_ops.ensure_role",
                   AsyncMock(return_value=False)):
            await admin_appeal_ops.run_block_for_appeal(event, 5, blocked=True)
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_appeal_not_found(self) -> None:
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        with patch("aemr_bot.handlers.admin_appeal_ops.ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_appeal_ops.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_appeal_ops.appeals_service.get_by_id",
                   AsyncMock(return_value=None)), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_appeal_ops.run_block_for_appeal(event, 999, blocked=True)
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "999" in text or "не найдено" in text.lower()

    @pytest.mark.asyncio
    async def test_block_happy_path(self) -> None:
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        appeal = SimpleNamespace(user=SimpleNamespace(max_user_id=42))
        write_audit = AsyncMock()
        with patch("aemr_bot.handlers.admin_appeal_ops.ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_appeal_ops.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_appeal_ops.appeals_service.get_by_id",
                   AsyncMock(return_value=appeal)), \
             patch("aemr_bot.handlers.admin_appeal_ops.users_service.set_blocked",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_appeal_ops.operators_service.write_audit",
                   write_audit), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_appeal_ops.run_block_for_appeal(event, 5, blocked=True)
        write_audit.assert_called_once()
        assert write_audit.call_args.kwargs["action"] == "block"

    @pytest.mark.asyncio
    async def test_unblock_uses_unblock_audit(self) -> None:
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        appeal = SimpleNamespace(user=SimpleNamespace(max_user_id=42))
        write_audit = AsyncMock()
        with patch("aemr_bot.handlers.admin_appeal_ops.ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_appeal_ops.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_appeal_ops.appeals_service.get_by_id",
                   AsyncMock(return_value=appeal)), \
             patch("aemr_bot.handlers.admin_appeal_ops.users_service.set_blocked",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_appeal_ops.operators_service.write_audit",
                   write_audit), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_appeal_ops.run_block_for_appeal(event, 5, blocked=False)
        assert write_audit.call_args.kwargs["action"] == "unblock"

    @pytest.mark.asyncio
    async def test_block_failure_message(self) -> None:
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        appeal = SimpleNamespace(user=SimpleNamespace(max_user_id=42))
        with patch("aemr_bot.handlers.admin_appeal_ops.ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_appeal_ops.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_appeal_ops.appeals_service.get_by_id",
                   AsyncMock(return_value=appeal)), \
             patch("aemr_bot.handlers.admin_appeal_ops.users_service.set_blocked",
                   AsyncMock(return_value=False)), \
             patch("aemr_bot.handlers.admin_appeal_ops.operators_service.write_audit",
                   AsyncMock()), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_appeal_ops.run_block_for_appeal(event, 5, blocked=True)
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "Не удалось" in text


# --- run_erase_for_appeal -----------------------------------------------------


class TestRunEraseForAppeal:
    @pytest.mark.asyncio
    async def test_not_it_returns(self) -> None:
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        with patch("aemr_bot.handlers.admin_appeal_ops.ensure_role",
                   AsyncMock(return_value=False)):
            await admin_appeal_ops.run_erase_for_appeal(event, 5)
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_appeal_not_found(self) -> None:
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        with patch("aemr_bot.handlers.admin_appeal_ops.ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_appeal_ops.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_appeal_ops.appeals_service.get_by_id",
                   AsyncMock(return_value=None)), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_appeal_ops.run_erase_for_appeal(event, 999)
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "999" in text or "не найдено" in text.lower()

    @pytest.mark.asyncio
    async def test_erase_happy_path(self) -> None:
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        appeal = SimpleNamespace(user=SimpleNamespace(max_user_id=42))
        write_audit = AsyncMock()
        with patch("aemr_bot.handlers.admin_appeal_ops.ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_appeal_ops.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_appeal_ops.appeals_service.get_by_id",
                   AsyncMock(return_value=appeal)), \
             patch("aemr_bot.handlers.admin_appeal_ops.users_service.erase_pdn",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_appeal_ops.operators_service.write_audit",
                   write_audit), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_appeal_ops.run_erase_for_appeal(event, 5)
        write_audit.assert_called_once()

    @pytest.mark.asyncio
    async def test_erase_failure_message(self) -> None:
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        appeal = SimpleNamespace(user=SimpleNamespace(max_user_id=42))
        with patch("aemr_bot.handlers.admin_appeal_ops.ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_appeal_ops.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_appeal_ops.appeals_service.get_by_id",
                   AsyncMock(return_value=appeal)), \
             patch("aemr_bot.handlers.admin_appeal_ops.users_service.erase_pdn",
                   AsyncMock(return_value=False)), \
             patch("aemr_bot.handlers.admin_appeal_ops.operators_service.write_audit",
                   AsyncMock()), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_appeal_ops.run_erase_for_appeal(event, 5)
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "не найден" in text.lower()
