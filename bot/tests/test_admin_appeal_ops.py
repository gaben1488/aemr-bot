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

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests._helpers import fake_session_scope as _fake_session_scope
from tests._helpers import make_event

pytest.importorskip("maxapi", reason="handlers тесты требуют maxapi")


def _make_event(*, chat_id: int = 555, user_id: int = 7) -> SimpleNamespace:
    # Тонкая обёртка над tests/_helpers.make_event — сохраняет файловые
    # дефолты, структуру события держит helper. callback нужен для
    # admin-action handler'ов.
    return make_event(chat_id=chat_id, user_id=user_id, with_callback=True)


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
             patch("aemr_bot.handlers.admin_appeal_ops.appeals_service.mark_in_progress",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.operator_reply.remember_reply_intent",
                   remember), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_appeal_ops.run_reply_intent(event, 5)
        remember.assert_called_once()
        # default is_final=True пробрасывается в remember_reply_intent
        assert remember.call_args.kwargs.get("is_final") is True
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "ответ" in text.lower()

    @pytest.mark.asyncio
    async def test_intermediate_intent_sets_is_final_false(self) -> None:
        """run_reply_intent(is_final=False) — промежуточный ответ
        (не закрывает обращение)."""
        from aemr_bot.db.models import AppealStatus
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        appeal = SimpleNamespace(
            id=7,
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
             patch("aemr_bot.handlers.admin_appeal_ops.appeals_service.mark_in_progress",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.operator_reply.remember_reply_intent",
                   remember), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_appeal_ops.run_reply_intent(event, 7, is_final=False)
        remember.assert_called_once()
        assert remember.call_args.kwargs.get("is_final") is False
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "промежуточ" in text.lower()


class TestRunReplyIntentRaceWarning:
    """P2 #22 — race rapid double-tap: предупреждение при перезаписи
    активного intent на другое обращение.
    """

    async def _run(self, *, existing_intent: tuple[int, bool, float] | None):
        from aemr_bot.db.models import AppealStatus
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        appeal = SimpleNamespace(
            id=20,
            status=AppealStatus.NEW.value,
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
             patch("aemr_bot.handlers.admin_appeal_ops.appeals_service.mark_in_progress",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_appeal_ops.get_user_id",
                   return_value=7), \
             patch("aemr_bot.handlers.operator_reply.remember_reply_intent",
                   MagicMock()), \
             patch("aemr_bot.services.wizard_registry.get_reply_intent",
                   return_value=existing_intent), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_appeal_ops.run_reply_intent(event, 20)
        return event

    @pytest.mark.asyncio
    async def test_no_existing_intent_no_warning(self) -> None:
        event = await self._run(existing_intent=None)
        texts_sent = [
            c.kwargs.get("text", "")
            for c in event.bot.send_message.call_args_list
        ]
        assert not any("отменена" in t.lower() for t in texts_sent)

    @pytest.mark.asyncio
    async def test_same_appeal_intent_no_warning(self) -> None:
        """Тот же appeal_id (повторный тап «Ответить» на той же карточке)
        — это нормальный case (TTL refresh), не предупреждаем."""
        # existing_intent on same appeal_id=20
        event = await self._run(existing_intent=(20, True, 1.0))
        texts_sent = [
            c.kwargs.get("text", "")
            for c in event.bot.send_message.call_args_list
        ]
        assert not any("отменена" in t.lower() for t in texts_sent)

    @pytest.mark.asyncio
    async def test_different_appeal_intent_warns_operator(self) -> None:
        """existing_intent на другое обращение → предупреждение,
        что предыдущий ответ перезаписан."""
        event = await self._run(existing_intent=(11, True, 1.0))
        texts_sent = [
            c.kwargs.get("text", "")
            for c in event.bot.send_message.call_args_list
        ]
        assert any(
            "отменена" in t.lower() and "#11" in t and "#20" in t
            for t in texts_sent
        ), texts_sent


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
                   AsyncMock(return_value="reopened")), \
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
                   AsyncMock(return_value="not_found")), \
             patch("aemr_bot.handlers.admin_appeal_ops.operators_service.write_audit",
                   write_audit), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_appeal_ops.run_reopen(event, 999)
        write_audit.assert_not_called()

    @pytest.mark.asyncio
    async def test_blocked_by_revoke_no_audit_informative_text(self) -> None:
        """blocked_by_revoke → audit не пишем, оператор видит понятное
        сообщение про ПДн-гард (не дезориентирующее «не найдено»)."""
        from aemr_bot.handlers import admin_appeal_ops
        from aemr_bot import texts

        event = _make_event()
        write_audit = AsyncMock()
        with patch("aemr_bot.handlers.admin_appeal_ops.ensure_operator",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_appeal_ops.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_appeal_ops.appeals_service.reopen",
                   AsyncMock(return_value="blocked_by_revoke")), \
             patch("aemr_bot.handlers.admin_appeal_ops.operators_service.write_audit",
                   write_audit), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_appeal_ops.run_reopen(event, 7)
        write_audit.assert_not_called()
        # хотя бы одно send_message с текстом про блокировку
        sent_text = event.bot.send_message.call_args.kwargs.get("text", "")
        expected = texts.OP_APPEAL_BLOCKED_BY_REVOKE.format(number=7)
        assert expected in sent_text or sent_text == expected

    @pytest.mark.asyncio
    async def test_already_open_no_audit(self) -> None:
        """already_open → no-op, audit не пишем."""
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        write_audit = AsyncMock()
        with patch("aemr_bot.handlers.admin_appeal_ops.ensure_operator",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_appeal_ops.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_appeal_ops.appeals_service.reopen",
                   AsyncMock(return_value="already_open")), \
             patch("aemr_bot.handlers.admin_appeal_ops.operators_service.write_audit",
                   write_audit), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_appeal_ops.run_reopen(event, 7)
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
             patch("aemr_bot.handlers.admin_appeal_ops.appeals_service.has_operator_message",
                   AsyncMock(return_value=False)), \
             patch("aemr_bot.handlers.admin_appeal_ops.appeals_service.close",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_appeal_ops.operators_service.write_audit",
                   AsyncMock()), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_appeal_ops.run_close(event, 5)
        event.bot.send_message.assert_called_once()
        # Sanity: без intermediate-reply подсказка про «Ответить и
        # закрыть» НЕ добавляется.
        sent_text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "промежуточный ответ" not in sent_text.lower()

    @pytest.mark.asyncio
    async def test_close_after_intermediate_reply_shows_hint(self) -> None:
        """P2 #23: close после intermediate reply → warning в тексте."""
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        write_audit = AsyncMock()
        with patch("aemr_bot.handlers.admin_appeal_ops.ensure_operator",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_appeal_ops.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_appeal_ops.appeals_service.has_operator_message",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_appeal_ops.appeals_service.close",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_appeal_ops.operators_service.write_audit",
                   write_audit), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_appeal_ops.run_close(event, 5)
        # Текст содержит подсказку про корректный путь.
        sent_text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "промежуточный ответ" in sent_text.lower()
        assert "ответить и закрыть" in sent_text.lower()
        # Audit пишет факт «закрыто после промежуточного».
        write_audit.assert_awaited_once()
        details = write_audit.call_args.kwargs.get("details")
        assert details == {"after_intermediate_reply": True}


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
