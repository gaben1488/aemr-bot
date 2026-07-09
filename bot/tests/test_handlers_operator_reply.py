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
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests._helpers import fake_session_scope as _fake_session_scope
from tests._helpers import make_event

pytest.importorskip("maxapi", reason="handlers тесты требуют maxapi")


def _make_event(*, chat_id: int = 100, user_id: int = 7) -> SimpleNamespace:
    # Обёртка над tests/_helpers.make_event. operator_reply-handler'ы
    # читают event.message.link и редактируют сообщения — доставляем
    # link и bot.edit_message поверх базовой фабрики.
    event = make_event(
        chat_id=chat_id, user_id=user_id, with_edit_message=True
    )
    event.message.link = None
    return event


def _fresh_appeal(*, user=None, appeal_id: int = 1) -> SimpleNamespace:
    if user is None:
        user = SimpleNamespace(
            is_blocked=False,
            first_name="Иван",
            phone="+79991234567",
            subscribed_broadcast=False,
            consent_pdn_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            consent_revoked_at=None,
            max_user_id=42,
        )
    appeal = SimpleNamespace(
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
    appeal.__dict__["messages"] = []
    return appeal


class TestReplyIntent:
    def test_remember_and_consume(self) -> None:
        from aemr_bot.handlers import operator_reply as opr

        # Очищаем перед тестом (state модуля).
        from aemr_bot.services import wizard_registry as _wr
        _wr._reply_intent.clear()
        opr.remember_reply_intent(operator_id=7, appeal_id=42)
        # consume теперь возвращает (appeal_id, is_final). Default is_final=True.
        assert opr.consume_reply_intent(7) == (42, True)
        # Второй вызов — пусто.
        assert opr.consume_reply_intent(7) is None

    def test_remember_and_consume_intermediate(self) -> None:
        """is_final=False (промежуточный ответ) сохраняется и возвращается."""
        from aemr_bot.handlers import operator_reply as opr
        from aemr_bot.services import wizard_registry as _wr
        _wr._reply_intent.clear()
        opr.remember_reply_intent(operator_id=7, appeal_id=42, is_final=False)
        assert opr.consume_reply_intent(7) == (42, False)

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


@pytest.fixture(autouse=True)
def _autopatch_operators_get(request):
    """SEC #6: _deliver_operator_reply ре-проверяет operators_service.get
    перед доставкой (защита от deactivated operator). Юнит-тесты в этом
    модуле гоняют через fake session — patch'им get на возврат активного
    оператора, чтобы security-чек не падал на MagicMock-сессии."""
    # Применяется только в этом модуле; не трогает другие тесты.
    if "test_handlers_operator_reply" not in request.node.nodeid:
        yield
        return
    live_op = SimpleNamespace(id=7, max_user_id=42, is_active=True)
    with patch(
        "aemr_bot.handlers.operator_reply.operators_service.get",
        AsyncMock(return_value=live_op),
    ):
        yield


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
        # is_final по умолчанию True → нет отдельной confirm-карточки
        # (убрана владельцем, см. TestIntermediateReplyConfirmText),
        # только доставка ответа жителю.
        assert event.bot.send_message.call_count == 1

    @pytest.mark.asyncio
    async def test_success_publishes_event_card_via_render(self) -> None:
        """DDD event-log: после доставки ответа жителю шлём НОВУЮ
        event-карточку (через admin_card.render). Edit оригинала
        больше не делается — каждое событие = новая запись."""
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event()
        event.bot.send_message = AsyncMock(
            side_effect=[SimpleNamespace(body=SimpleNamespace(mid="out-1")), None]
        )
        appeal = MagicMock(id=1)
        operator = MagicMock(id=7, max_user_id=42)
        fresh_appeal = _fresh_appeal()
        fresh_appeal.admin_message_id = "admin-mid-1"
        fresh_appeal.last_admin_card_mid = "admin-mid-1"
        fresh_appeal.closed_due_to_revoke = False

        render_mock = AsyncMock(return_value="new-event-mid")
        opr._recent_replies.clear()
        with patch.object(opr.cfg, "answer_max_chars", 1000), \
             patch("aemr_bot.handlers.operator_reply.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.operator_reply.appeals_service.get_by_id",
                   AsyncMock(side_effect=[fresh_appeal, fresh_appeal])), \
             patch("aemr_bot.handlers.operator_reply.appeals_service.get_by_id_with_messages",
                   AsyncMock(return_value=fresh_appeal)), \
             patch("aemr_bot.handlers.operator_reply.appeals_service.add_operator_message",
                   AsyncMock()), \
             patch("aemr_bot.handlers.operator_reply.operators_service.write_audit",
                   AsyncMock()), \
             patch("aemr_bot.handlers.operator_reply._is_reply_success_recorded",
                   AsyncMock(return_value=False)), \
             patch("aemr_bot.handlers.operator_reply._mark_reply_success_recorded",
                   AsyncMock()), \
             patch("aemr_bot.services.admin_card.render", render_mock), \
             patch("aemr_bot.config.settings.admin_group_id", 555):
            handled = await opr._deliver_operator_reply(
                event, appeal=appeal, operator=operator,
                text="ответ", audit_action="reply",
            )

        assert handled is True
        # Новая event-log семантика: render публикует НОВУЮ карточку.
        render_mock.assert_awaited_once()
        # edit_message НЕ вызывается напрямую (старая семантика).


class TestIntermediateReplyConfirmText:
    """Развязка confirm-сообщения оператора: финальный vs промежуточный.

    Регрессия — раньше шёл ADMIN_REPLY_DELIVERED = «Обращение #N закрыто»
    даже для is_final=False. Это путало оператора (он понимал, что
    житель остаётся в работе, но карточка говорила обратное).

    2026-07: для is_final=True отдельное confirm-сообщение
    (ADMIN_REPLY_DELIVERED_FINAL, «Финальный ответ отправлен жителю…»)
    убрано владельцем — оно дублировало event_header свежей карточки
    обращения (`admin_card.render`, замоканный здесь). Для is_final=False
    confirm-сообщение (ADMIN_REPLY_DELIVERED_INTERMEDIATE) сохранено —
    несёт другую информацию, которой нет в event_header.
    """

    async def _run_deliver(self, *, is_final: bool) -> SimpleNamespace:
        from aemr_bot.handlers import operator_reply as opr

        event = _make_event()
        event.bot.send_message = AsyncMock(
            side_effect=[SimpleNamespace(body=SimpleNamespace(mid="out-1")), None]
        )
        appeal = MagicMock(id=42)
        operator = MagicMock(id=7, max_user_id=42)
        fresh_appeal = _fresh_appeal(appeal_id=42)
        fresh_appeal.admin_message_id = "admin-mid-1"
        fresh_appeal.last_admin_card_mid = "admin-mid-1"
        fresh_appeal.closed_due_to_revoke = False

        opr._recent_replies.clear()
        with patch.object(opr.cfg, "answer_max_chars", 1000), \
             patch("aemr_bot.handlers.operator_reply.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.operator_reply.appeals_service.get_by_id",
                   AsyncMock(side_effect=[fresh_appeal, fresh_appeal])), \
             patch("aemr_bot.handlers.operator_reply.appeals_service.get_by_id_with_messages",
                   AsyncMock(return_value=fresh_appeal)), \
             patch("aemr_bot.handlers.operator_reply.appeals_service.add_operator_message",
                   AsyncMock()), \
             patch("aemr_bot.handlers.operator_reply.operators_service.write_audit",
                   AsyncMock()), \
             patch("aemr_bot.handlers.operator_reply._is_reply_success_recorded",
                   AsyncMock(return_value=False)), \
             patch("aemr_bot.handlers.operator_reply._mark_reply_success_recorded",
                   AsyncMock()), \
             patch("aemr_bot.services.admin_card.render",
                   AsyncMock(return_value="new-event-mid")), \
             patch("aemr_bot.config.settings.admin_group_id", 555):
            await opr._deliver_operator_reply(
                event, appeal=appeal, operator=operator,
                text="ответ", audit_action="reply",
                is_final=is_final,
            )
        return event

    @pytest.mark.asyncio
    async def test_final_reply_sends_no_extra_confirm_card(self) -> None:
        """Финальный ответ: НЕТ отдельной confirm-карточки оператору
        (`chat_id`-сообщения) — только доставка жителю (`user_id`).
        Убрано владельцем: дублировало event_header карточки обращения,
        которую только что опубликовал `admin_card.render` (замокан)."""
        event = await self._run_deliver(is_final=True)
        confirm_calls = [
            c for c in event.bot.send_message.await_args_list
            if "chat_id" in c.kwargs and c.kwargs.get("text")
        ]
        assert not confirm_calls, (
            f"избыточная confirm-карточка оператору не должна отправляться "
            f"для финального ответа; call(s)={confirm_calls}"
        )
        # Единственный вызов send_message — доставка жителю по user_id.
        delivery_calls = [
            c for c in event.bot.send_message.await_args_list
            if c.kwargs.get("user_id") is not None
        ]
        assert len(delivery_calls) == 1

    @pytest.mark.asyncio
    async def test_intermediate_reply_says_in_progress(self) -> None:
        event = await self._run_deliver(is_final=False)
        confirm_calls = [
            c for c in event.bot.send_message.await_args_list
            if "chat_id" in c.kwargs and c.kwargs.get("text")
        ]
        assert confirm_calls
        text = confirm_calls[-1].kwargs.get("text", "")
        # Главное: НЕТ слова «закрыт».
        assert "закрыт" not in text.lower(), text
        # И есть маркер промежуточности.
        assert (
            "промежуточный" in text.lower()
            or "в работе" in text.lower()
        ), text
        assert "#42" in text


class TestReplyRejectionBeforeDelivery:
    """_reply_rejection_before_delivery — чистая функция guard'а перед
    доставкой. Возвращает текст отказа либо None (доставка разрешена).
    Фиксирует матрицу согласия по 152-ФЗ ст. 21 ч. 5."""

    def test_healthy_appeal_allows_delivery(self) -> None:
        from aemr_bot.handlers.operator_reply import (
            _reply_rejection_before_delivery,
        )

        assert _reply_rejection_before_delivery(
            fresh_appeal=_fresh_appeal(), appeal_id=1
        ) is None

    def test_none_appeal_rejected(self) -> None:
        from aemr_bot.handlers.operator_reply import (
            _reply_rejection_before_delivery,
        )

        msg = _reply_rejection_before_delivery(fresh_appeal=None, appeal_id=1)
        assert msg is not None and "не найдены" in msg

    def test_appeal_without_user_rejected(self) -> None:
        from aemr_bot.handlers.operator_reply import (
            _reply_rejection_before_delivery,
        )

        appeal = _fresh_appeal()
        appeal.user = None
        assert _reply_rejection_before_delivery(
            fresh_appeal=appeal, appeal_id=1
        ) is not None

    def test_closed_appeal_rejected(self) -> None:
        from aemr_bot.handlers.operator_reply import (
            _reply_rejection_before_delivery,
        )

        appeal = _fresh_appeal()
        appeal.status = "closed"
        msg = _reply_rejection_before_delivery(fresh_appeal=appeal, appeal_id=1)
        assert msg is not None and "закрыто" in msg

    def test_blocked_user_rejected(self) -> None:
        from aemr_bot.handlers.operator_reply import (
            _reply_rejection_before_delivery,
        )

        appeal = _fresh_appeal()
        appeal.user.is_blocked = True
        assert _reply_rejection_before_delivery(
            fresh_appeal=appeal, appeal_id=1
        ) is not None

    def test_erased_user_rejected(self) -> None:
        from aemr_bot.handlers.operator_reply import (
            _reply_rejection_before_delivery,
        )

        appeal = _fresh_appeal()
        appeal.user.first_name = "Удалено"
        assert _reply_rejection_before_delivery(
            fresh_appeal=appeal, appeal_id=1
        ) is not None

    def test_no_consent_ever_rejected(self) -> None:
        from aemr_bot.handlers.operator_reply import (
            _reply_rejection_before_delivery,
        )

        appeal = _fresh_appeal()
        appeal.user.consent_pdn_at = None
        appeal.user.consent_revoked_at = None
        assert _reply_rejection_before_delivery(
            fresh_appeal=appeal, appeal_id=1
        ) is not None

    def test_revoked_then_new_appeal_rejected(self) -> None:
        from aemr_bot.handlers.operator_reply import (
            _reply_rejection_before_delivery,
        )

        # Согласие отозвано 2026-04-01, обращение создано 2026-05-01 —
        # ПОСЛЕ отзыва → доставка запрещена.
        appeal = _fresh_appeal()
        appeal.user.consent_pdn_at = None
        appeal.user.consent_revoked_at = datetime(
            2026, 4, 1, tzinfo=timezone.utc
        )
        assert _reply_rejection_before_delivery(
            fresh_appeal=appeal, appeal_id=1
        ) is not None

    def test_revoked_after_older_appeal_allows_final_reply(self) -> None:
        from aemr_bot.handlers.operator_reply import (
            _reply_rejection_before_delivery,
        )

        # Обращение создано 2026-05-01, согласие отозвано позже
        # 2026-05-10 → финальный ответ по принятому ранее обращению
        # разрешён.
        appeal = _fresh_appeal()
        appeal.user.consent_pdn_at = None
        appeal.user.consent_revoked_at = datetime(
            2026, 5, 10, tzinfo=timezone.utc
        )
        assert _reply_rejection_before_delivery(
            fresh_appeal=appeal, appeal_id=1
        ) is None


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
        event.bot.send_message = AsyncMock()
        with patch.object(opr.cfg, "admin_group_id", 555):
            result = await opr.handle_command_reply(event, appeal_id=1, text="test")

        # Нет автора → выходим до доставки: жителю ничего не уходит.
        assert result is None
        event.bot.send_message.assert_not_called()

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
