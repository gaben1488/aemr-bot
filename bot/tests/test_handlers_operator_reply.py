"""Тесты handlers/operator_reply.py — ответы операторов и intent dedupe.

Локально skip без maxapi; в CI работает.

Покрываем:
- remember_reply_intent / consume_reply_intent / drop_reply_intent
- recent-success dedupe: проверка не отравляет retry, запись только после success
- _mid_from_link / _extract_reply_target_mid (Pydantic / dict)
- _deliver_operator_reply: too_long, blocked/no-consent, vanished, send error,
  DB/audit write error, success
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("maxapi", reason="handlers тесты требуют maxapi")


def _make_event(*, chat_id: int = 100, user_id: int = 7) -> SimpleNamespace:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.edit_message = AsyncMock()
    return SimpleNamespace(
        bot=bot,
        message=SimpleNamespace(
            sender=SimpleNamespace(user_id=user_id),
            recipient=SimpleNamespace(chat_id=chat_id),
            body=SimpleNamespace(text="", attachments=[], mid="m-1"),
            link=None,
        ),
    )


@asynccontextmanager
async def _fake_session_scope():
    yield MagicMock()


def _fresh_appeal(*, user=None, appeal_id: int = 1) -> SimpleNamespace:
    if user is None:
        user = SimpleNamespace(
            is_blocked=False,
            first_name="Иван",
            consent_pdn_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            consent_revoked_at=None,
            max_user_id=42,
        )
    return SimpleNamespace(
        id=appeal_id,
        user=user,
        created_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        topic="Дороги",
        locality="Елизово",
        address="ул. Ленина, д. 1",
        status="new",
        summary="яма",
        attachments=[],
    )


class TestReplyIntent:
    def test_remember_and_consume(self) -> None:
        from aemr_bot.handlers import operator_reply as opr

        # Очищаем перед тестом (state модуля).
        from aemr_bot.services import wizard_registry as _wr
        _wr._reply_intent.clear()
        opr.remember_reply_intent(operator_id=7, appeal_id=42)
        assert opr.consume_reply_intent(7) == 42
        # Второй вызов — пусто.
        assert opr.consume_reply_intent(7) is None

    def test_drop_returns_appeal_id(self) -> None:
        from aemr_bot.handlers import operator_reply as opr

        from aemr_bot.services import wizard_registry as _wr
        _wr._reply_intent.clear()
        opr.remember_reply_intent(operator_id=8, appeal_id=99)
        assert opr.drop_reply_intent(8) == 99
        assert opr.consume_reply_intent(8) is None

    def test_drop_when_no_intent(self) -> None:
        from aemr_bot.handlers import operator_reply as opr

        from aemr_bot.services import wizard_registry as _wr
        _wr._reply_intent.clear()
        assert opr.drop_reply_intent(123) is None

    def test_intent_expires(self) -> None:
        from aemr_bot.handlers import operator_reply as opr
        from aemr_bot.services import wizard_registry as _wr

        _wr._reply_intent.clear()
        # Ставим истёкшее намерение вручную через registry API.
        _wr.set_reply_intent(5, 10, time.monotonic() - 1.0)
        assert opr.consume_reply_intent(5) is None


class TestRecentSuccessfulReplyDedupe:
    def test_first_reply_is_unique(self) -> None:
        from aemr_bot.handlers import operator_reply as opr

        opr._recent_replies.clear()
        assert opr._is_duplicate_reply(1, 100, "text-A") is False

    def test_check_does_not_poison_retry(self) -> None:
        from aemr_bot.handlers import operator_reply as opr

        opr._recent_replies.clear()
        assert opr._is_duplicate_reply(1, 100, "text-A") is False
        # Вторая проверка тоже False: ключ не занят, пока ответ не завершился успешно.
        assert opr._is_duplicate_reply(1, 100, "text-A") is False

    def test_successful_same_text_in_window_is_dupe(self) -> None:
        from aemr_bot.handlers import operator_reply as opr

        opr._recent_replies.clear()
        opr._remember_successful_reply(1, 100, "text-A")
        assert opr._is_duplicate_reply(1, 100, "text-A") is True

    def test_different_text_not_dupe(self) -> None:
        from aemr_bot.handlers import operator_reply as opr

        opr._recent_replies.clear()
        opr._remember_successful_reply(1, 100, "first")
        assert opr._is_duplicate_reply(1, 100, "second") is False


class TestMidFromLink:
    def test_pydantic_form_with_inner_message(self) -> None:
        from aemr_bot.handlers import operator_reply as opr

        link = SimpleNamespace(message=SimpleNamespace(mid="MID-X"))
        assert opr._mid_from_link(link) == "MID-X"

    def test_dict_form_with_inner_message(self) -> None:
        from aemr_bot.handlers import operator_reply as opr

        link = {"message": {"mid": "MID-Y"}}
        assert opr._mid_from_link(link) == "MID-Y"

    def test_dict_form_with_top_level_mid(self) -> None:
        from aemr_bot.handlers import operator_reply as opr

        link = {"mid": "MID-Z"}
        assert opr._mid_from_link(link) == "MID-Z"

    def test_legacy_top_level_mid(self) -> None:
        from aemr_bot.handlers import operator_reply as opr

        link = SimpleNamespace(mid="MID-LEGACY", message=None)
        assert opr._mid_from_link(link) == "MID-LEGACY"

    def test_no_mid_returns_none(self) -> None:
        from aemr_bot.handlers import operator_reply as opr

        link = SimpleNamespace(message=None)
        assert opr._mid_from_link(link) is None


class TestExtractReplyTargetMid:
    def test_no_link_returns_none(self) -> None:
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event()
        event.message.link = None
        assert opr._extract_reply_target_mid(event) is None

    def test_non_reply_link_returns_none(self) -> None:
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event()
        # type='forward' — не reply
        event.message.link = SimpleNamespace(
            type="forward", message=SimpleNamespace(mid="X")
        )
        assert opr._extract_reply_target_mid(event) is None

    def test_reply_link_returns_mid(self) -> None:
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event()
        event.message.link = SimpleNamespace(
            type="reply", message=SimpleNamespace(mid="MID-1")
        )
        assert opr._extract_reply_target_mid(event) == "MID-1"


class TestDeliverOperatorReply:
    @pytest.mark.asyncio
    async def test_too_long_text_rejected(self) -> None:
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event()
        appeal = MagicMock()
        appeal.id = 1
        operator = MagicMock()
        operator.id = 7
        operator.max_user_id = 42

        with patch.object(opr.cfg, "answer_max_chars", 10):
            handled = await opr._deliver_operator_reply(
                event, appeal=appeal, operator=operator,
                text="x" * 50, audit_action="reply",
            )
        assert handled is True
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "слишком" in text.lower() or "long" in text.lower() or "лимит" in text.lower() or "10" in text

    @pytest.mark.asyncio
    async def test_in_memory_dupe_skipped(self) -> None:
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event()
        appeal = MagicMock()
        appeal.id = 1
        operator = MagicMock()
        operator.id = 7
        operator.max_user_id = 42

        # Заранее запоминаем только успешный ответ.
        opr._recent_replies.clear()
        opr._remember_successful_reply(operator.id, appeal.id, "X")
        with patch.object(opr.cfg, "answer_max_chars", 1000):
            handled = await opr._deliver_operator_reply(
                event, appeal=appeal, operator=operator,
                text="X", audit_action="reply",
            )
        assert handled is True
        # Не должно быть send_message — дубль молча отбит.
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_success_key_dupe_skipped(self) -> None:
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event()
        appeal = MagicMock(id=1)
        operator = MagicMock(id=7, max_user_id=42)

        with patch.object(opr.cfg, "answer_max_chars", 1000), \
             patch("aemr_bot.handlers.operator_reply._is_reply_success_recorded",
                   AsyncMock(return_value=True)):
            handled = await opr._deliver_operator_reply(
                event, appeal=appeal, operator=operator,
                text="уже ушло", audit_action="reply",
            )
        assert handled is True
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_appeal_user_blocked_refuses(self) -> None:
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event()
        appeal = MagicMock()
        appeal.id = 1
        operator = MagicMock()
        operator.id = 7
        operator.max_user_id = 42

        fresh_user = SimpleNamespace(
            is_blocked=True,
            first_name="Иван",
            consent_pdn_at=None,
            consent_revoked_at=None,
            max_user_id=42,
        )
        fresh_appeal = _fresh_appeal(user=fresh_user)
        opr._recent_replies.clear()
        with patch.object(opr.cfg, "answer_max_chars", 1000), \
             patch("aemr_bot.handlers.operator_reply.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.operator_reply.appeals_service.get_by_id",
                   AsyncMock(return_value=fresh_appeal)), \
             patch("aemr_bot.handlers.operator_reply._is_reply_success_recorded",
                   AsyncMock(return_value=False)):
            handled = await opr._deliver_operator_reply(
                event, appeal=appeal, operator=operator,
                text="привет", audit_action="reply",
            )
        assert handled is True
        # Шлёт в админ-чат предупреждение «не могу доставить».
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "не могу доставить" in text.lower() or "Не могу" in text

    @pytest.mark.asyncio
    async def test_revoked_consent_allows_final_reply_for_older_appeal(self) -> None:
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event()
        event.bot.send_message = AsyncMock(
            side_effect=[SimpleNamespace(body=SimpleNamespace(mid="out-1")), None]
        )
        appeal = MagicMock()
        appeal.id = 1
        operator = MagicMock()
        operator.id = 7
        operator.max_user_id = 42

        fresh_user = SimpleNamespace(
            is_blocked=False,
            first_name="Иван",
            consent_pdn_at=None,
            consent_revoked_at=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
            max_user_id=42,
        )
        fresh_appeal = _fresh_appeal(user=fresh_user)
        opr._recent_replies.clear()
        with patch.object(opr.cfg, "answer_max_chars", 1000), \
             patch("aemr_bot.handlers.operator_reply.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.operator_reply.appeals_service.get_by_id",
                   AsyncMock(return_value=fresh_appeal)), \
             patch("aemr_bot.handlers.operator_reply.appeals_service.add_operator_message",
                   AsyncMock()) as add_message, \
             patch("aemr_bot.handlers.operator_reply.operators_service.write_audit",
                   AsyncMock()), \
             patch("aemr_bot.handlers.operator_reply._is_reply_success_recorded",
                   AsyncMock(return_value=False)), \
             patch("aemr_bot.handlers.operator_reply._mark_reply_success_recorded",
                   AsyncMock()):
            handled = await opr._deliver_operator_reply(
                event, appeal=appeal, operator=operator,
                text="ответ", audit_action="reply",
            )

        assert handled is True
        assert event.bot.send_message.call_args_list[0].kwargs["user_id"] == 42
        add_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_appeal_vanished_in_db_returns_handled(self) -> None:
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event()
        appeal = MagicMock()
        appeal.id = 1
        operator = MagicMock()
        operator.id = 7
        operator.max_user_id = 42

        opr._recent_replies.clear()
        with patch.object(opr.cfg, "answer_max_chars", 1000), \
             patch("aemr_bot.handlers.operator_reply.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.operator_reply.appeals_service.get_by_id",
                   AsyncMock(return_value=None)), \
             patch("aemr_bot.handlers.operator_reply._is_reply_success_recorded",
                   AsyncMock(return_value=False)):
            handled = await opr._deliver_operator_reply(
                event, appeal=appeal, operator=operator,
                text="hi", audit_action="reply",
            )
        assert handled is True
        text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "не найдены" in text.lower() or "не могу" in text.lower()

    @pytest.mark.asyncio
    async def test_delivery_error_does_not_poison_retry(self) -> None:
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event()
        event.bot.send_message = AsyncMock(side_effect=RuntimeError("max down"))
        appeal = MagicMock(id=1)
        operator = MagicMock(id=7, max_user_id=42)
        fresh_appeal = _fresh_appeal()

        opr._recent_replies.clear()
        with patch.object(opr.cfg, "answer_max_chars", 1000), \
             patch("aemr_bot.handlers.operator_reply.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.operator_reply.appeals_service.get_by_id",
                   AsyncMock(return_value=fresh_appeal)), \
             patch("aemr_bot.handlers.operator_reply._is_reply_success_recorded",
                   AsyncMock(return_value=False)), \
             patch("aemr_bot.handlers.operator_reply._mark_reply_success_recorded",
                   AsyncMock()) as mark_success:
            handled = await opr._deliver_operator_reply(
                event, appeal=appeal, operator=operator,
                text="попытка", audit_action="reply",
            )

        assert handled is True
        mark_success.assert_not_called()
        assert opr._is_duplicate_reply(operator.id, appeal.id, "попытка") is False

    @pytest.mark.asyncio
    async def test_db_write_error_after_delivery_is_traceable_and_not_marked_success(self) -> None:
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event()
        event.bot.send_message = AsyncMock(
            side_effect=[SimpleNamespace(body=SimpleNamespace(mid="out-1")), None]
        )
        appeal = MagicMock(id=1)
        operator = MagicMock(id=7, max_user_id=42)
        fresh_appeal = _fresh_appeal()

        opr._recent_replies.clear()
        with patch.object(opr.cfg, "answer_max_chars", 1000), \
             patch("aemr_bot.handlers.operator_reply.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.operator_reply.appeals_service.get_by_id",
                   AsyncMock(side_effect=[fresh_appeal, fresh_appeal])), \
             patch("aemr_bot.handlers.operator_reply.appeals_service.add_operator_message",
                   AsyncMock(side_effect=RuntimeError("db write failed"))), \
             patch("aemr_bot.handlers.operator_reply.operators_service.write_audit",
                   AsyncMock()), \
             patch("aemr_bot.handlers.operator_reply._is_reply_success_recorded",
                   AsyncMock(return_value=False)), \
             patch("aemr_bot.handlers.operator_reply._mark_reply_success_recorded",
                   AsyncMock()) as mark_success:
            handled = await opr._deliver_operator_reply(
                event, appeal=appeal, operator=operator,
                text="ответ", audit_action="reply",
            )

        assert handled is True
        mark_success.assert_not_called()
        assert opr._is_duplicate_reply(operator.id, appeal.id, "ответ") is False
        assert event.bot.send_message.call_count == 2
        warning_text = event.bot.send_message.call_args.kwargs.get("text", "")
        assert "доставлен" in warning_text.lower()
        assert "баз" in warning_text.lower() or "audit" in warning_text.lower()

    @pytest.mark.asyncio
    async def test_success_marks_source_key_and_recent_success(self) -> None:
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event()
        event.bot.send_message = AsyncMock(
            side_effect=[SimpleNamespace(body=SimpleNamespace(mid="out-1")), None]
        )
        appeal = MagicMock(id=1)
        operator = MagicMock(id=7, max_user_id=42)
        fresh_appeal = _fresh_appeal()

        opr._recent_replies.clear()
        with patch.object(opr.cfg, "answer_max_chars", 1000), \
             patch("aemr_bot.handlers.operator_reply.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.operator_reply.appeals_service.get_by_id",
                   AsyncMock(side_effect=[fresh_appeal, fresh_appeal])), \
             patch("aemr_bot.handlers.operator_reply.appeals_service.add_operator_message",
                   AsyncMock()) as add_message, \
             patch("aemr_bot.handlers.operator_reply.operators_service.write_audit",
                   AsyncMock()) as write_audit, \
             patch("aemr_bot.handlers.operator_reply._is_reply_success_recorded",
                   AsyncMock(return_value=False)), \
             patch("aemr_bot.handlers.operator_reply._mark_reply_success_recorded",
                   AsyncMock()) as mark_success:
            handled = await opr._deliver_operator_reply(
                event, appeal=appeal, operator=operator,
                text="ответ", audit_action="reply",
            )

        assert handled is True
        add_message.assert_called_once()
        write_audit.assert_called_once()
        mark_success.assert_called_once()
        assert opr._is_duplicate_reply(operator.id, appeal.id, "ответ") is True
        assert event.bot.send_message.call_count == 2

    @pytest.mark.asyncio
    async def test_success_refreshes_original_admin_card(self) -> None:
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event()
        event.bot.send_message = AsyncMock(
            side_effect=[SimpleNamespace(body=SimpleNamespace(mid="out-1")), None]
        )
        appeal = MagicMock(id=1)
        operator = MagicMock(id=7, max_user_id=42)
        fresh_appeal = _fresh_appeal()
        fresh_appeal.admin_message_id = "admin-mid-1"
        fresh_appeal.closed_due_to_revoke = False

        opr._recent_replies.clear()
        with patch.object(opr.cfg, "answer_max_chars", 1000), \
             patch("aemr_bot.handlers.operator_reply.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.operator_reply.appeals_service.get_by_id",
                   AsyncMock(side_effect=[fresh_appeal, fresh_appeal])), \
             patch("aemr_bot.handlers.operator_reply.appeals_service.add_operator_message",
                   AsyncMock()), \
             patch("aemr_bot.handlers.operator_reply.operators_service.write_audit",
                   AsyncMock()), \
             patch("aemr_bot.handlers.operator_reply._is_reply_success_recorded",
                   AsyncMock(return_value=False)), \
             patch("aemr_bot.handlers.operator_reply._mark_reply_success_recorded",
                   AsyncMock()), \
             patch("aemr_bot.handlers.operator_reply.card_format.admin_card",
                   return_value="обновлённая карточка"):
            handled = await opr._deliver_operator_reply(
                event, appeal=appeal, operator=operator,
                text="ответ", audit_action="reply",
            )

        assert handled is True
        event.bot.edit_message.assert_called_once()
        kwargs = event.bot.edit_message.call_args.kwargs
        assert kwargs["message_id"] == "admin-mid-1"
        assert kwargs["text"] == "обновлённая карточка"
        assert kwargs["attachments"]


class TestHandleCommandReply:
    @pytest.mark.asyncio
    async def test_skips_outside_admin_chat(self) -> None:
        from aemr_bot.handlers import operator_reply as opr

        # event.chat_id != admin_group_id
        event = _make_event(chat_id=999)
        with patch.object(opr.cfg, "admin_group_id", 555):
            await opr.handle_command_reply(event, appeal_id=1, text="test")
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_no_user_id(self) -> None:
        from aemr_bot.handlers import operator_reply as opr

        # Событие без user_id.
        event = SimpleNamespace(
            bot=MagicMock(),
            message=SimpleNamespace(
                sender=None,
                recipient=SimpleNamespace(chat_id=555),
            ),
        )
        with patch.object(opr.cfg, "admin_group_id", 555):
            await opr.handle_command_reply(event, appeal_id=1, text="test")

    @pytest.mark.asyncio
    async def test_unauthorized_user_gets_op_not_authorized(self) -> None:
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event(chat_id=555, user_id=7)
        with patch.object(opr.cfg, "admin_group_id", 555), \
             patch("aemr_bot.handlers.operator_reply.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.operator_reply.operators_service.get",
                   AsyncMock(return_value=None)):
            await opr.handle_command_reply(event, appeal_id=1, text="test")
        event.bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_appeal_not_found(self) -> None:
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event(chat_id=555, user_id=7)
        operator = SimpleNamespace(id=7, max_user_id=42)
        with patch.object(opr.cfg, "admin_group_id", 555), \
             patch("aemr_bot.handlers.operator_reply.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.operator_reply.operators_service.get",
                   AsyncMock(return_value=operator)), \
             patch("aemr_bot.handlers.operator_reply.appeals_service.get_by_id",
                   AsyncMock(return_value=None)):
            await opr.handle_command_reply(event, appeal_id=999, text="test")
        event.bot.send_message.assert_called_once()
