"""Тесты для handlers/admin_appeal_ops — действия оператора над
конкретным обращением (выделено из admin_commands рефакторингом
2026-05-10).

Локально skip без maxapi; в CI работает (services мокаются).

Покрываем:
- run_reply_intent: auth/consent/appeal guards, durable draft target/type,
  replayed input and stale cancellation
- run_reply_cancel: drop intent, no operator
- run_reopen: not operator, ok, not found
- run_close: not operator, ok, not found
- run_block_for_appeal: not it, appeal not found, ok blocked/unblocked,
  failure
- run_erase_for_appeal: not it, appeal not found, ok, failure
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
    @pytest.fixture
    def draft_flow(self, monkeypatch):
        from aemr_bot.handlers import operator_draft as handler

        operator = SimpleNamespace(id=70, max_user_id=7)
        appeal = SimpleNamespace(id=5, status="new", user=SimpleNamespace(
            is_blocked=False, consent_pdn_at=object(),
        ))
        draft = SimpleNamespace(operator_id=70, appeal_id=5, token="saved", is_final=True)
        mocks = SimpleNamespace(
            operator=AsyncMock(return_value=operator),
            appeal=AsyncMock(return_value=appeal),
            get=AsyncMock(return_value=None), start=AsyncMock(return_value=draft),
            mark=AsyncMock(), show=AsyncMock(), ack=AsyncMock(),
        )
        monkeypatch.setattr(handler, "session_scope", _fake_session_scope)
        monkeypatch.setattr(handler, "get_operator", mocks.operator)
        monkeypatch.setattr(handler, "is_admin_chat", lambda event: True)
        monkeypatch.setattr(handler.appeals, "get_by_id", mocks.appeal)
        monkeypatch.setattr(handler.appeals, "mark_in_progress", mocks.mark)
        monkeypatch.setattr(handler.operator_drafts, "get", mocks.get)
        monkeypatch.setattr(handler.operator_drafts, "start", mocks.start)
        monkeypatch.setattr(handler, "show", mocks.show)
        monkeypatch.setattr(handler, "ack_callback", mocks.ack)
        return mocks

    @pytest.mark.asyncio
    @pytest.mark.parametrize("authorized", ["outside_chat", "not_operator"])
    async def test_unauthorized_cannot_begin_draft(self, monkeypatch, draft_flow, authorized):
        from aemr_bot.handlers import admin_appeal_ops, operator_draft

        if authorized == "outside_chat":
            monkeypatch.setattr(operator_draft, "is_admin_chat", lambda event: False)
        else:
            draft_flow.operator.return_value = None
        await admin_appeal_ops.run_reply_intent(_make_event(), 5)
        draft_flow.appeal.assert_not_awaited()
        draft_flow.start.assert_not_awaited()
        draft_flow.show.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("condition", ["missing", "closed", "answered", "blocked", "no_consent"])
    async def test_unavailable_target_cannot_create_draft(self, draft_flow, condition):
        from aemr_bot.handlers import admin_appeal_ops

        appeal = draft_flow.appeal.return_value
        if condition == "missing":
            draft_flow.appeal.return_value = None
        elif condition in {"closed", "answered"}:
            appeal.status = condition
        elif condition == "blocked":
            appeal.user.is_blocked = True
        else:
            appeal.user.consent_pdn_at = None
        event = _make_event()
        await admin_appeal_ops.run_reply_intent(event, 5)
        draft_flow.start.assert_not_awaited()
        draft_flow.mark.assert_not_awaited()
        draft_flow.show.assert_not_awaited()
        assert "недоступен" in draft_flow.ack.await_args.args[1]
        event.bot.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("is_final", [True, False])
    async def test_new_draft_preserves_target_and_reply_type(self, draft_flow, is_final):
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        await admin_appeal_ops.run_reply_intent(event, 5, is_final=is_final)
        assert draft_flow.start.await_args.args[1:] == (70, 5)
        assert draft_flow.start.await_args.kwargs == {"is_final": is_final}
        draft_flow.mark.assert_awaited_once()
        draft_flow.show.assert_awaited_once_with(
            event, draft_flow.start.return_value, notice="", new=True,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("target", [5, 20])
    async def test_existing_draft_keeps_original_target_and_type(self, draft_flow, target):
        from aemr_bot.handlers import admin_appeal_ops

        existing = SimpleNamespace(operator_id=70, appeal_id=5, is_final=False, token="old")
        draft_flow.get.return_value = existing
        event = _make_event()
        await admin_appeal_ops.run_reply_intent(event, target, is_final=True)
        draft_flow.start.assert_not_awaited()
        draft_flow.mark.assert_not_awaited()
        draft_flow.show.assert_awaited_once()
        assert draft_flow.show.await_args.args == (event, existing)
        assert "не изменены" in draft_flow.show.await_args.kwargs["notice"]
        assert existing.appeal_id == 5 and existing.is_final is False and existing.token == "old"


# --- run_reply_cancel ---------------------------------------------------------


class TestRunReplyCancel:
    @pytest.mark.asyncio
    async def test_not_operator_returns(self) -> None:
        """P1 #6: гейт ensure_operator — не-оператор не должен ни
        снимать reply-intent, ни получать подтверждение отмены."""
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        with patch("aemr_bot.handlers.admin_appeal_ops.ensure_operator",
                   AsyncMock(return_value=False)), \
             patch("aemr_bot.handlers.admin_appeal_ops.ack_callback", AsyncMock()) as ack, \
             patch("aemr_bot.handlers.operator_reply.drop_reply_intent") as drop:
            await admin_appeal_ops.run_reply_cancel(event)
        drop.assert_not_called()
        event.bot.send_message.assert_not_called()
        ack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_drops_intent_and_confirms(self) -> None:
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        with patch("aemr_bot.handlers.admin_appeal_ops.ensure_operator",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.operator_reply.drop_reply_intent",
                   return_value=42), \
             patch("aemr_bot.services.wizard_registry.reply_intent_token_matches",
                   return_value=True), \
             patch("aemr_bot.handlers.admin_appeal_ops.ack_callback", AsyncMock()) as ack:
            await admin_appeal_ops.run_reply_cancel(event, "current")
        assert "42" in ack.await_args.args[1] and "отменён" in ack.await_args.args[1].lower()
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_old_cancel_button_does_not_cancel_current_intent(self) -> None:
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        with patch("aemr_bot.handlers.admin_appeal_ops.ensure_operator",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.operator_reply.drop_reply_intent",
                   return_value=42) as drop, \
             patch("aemr_bot.services.wizard_registry.reply_intent_token_matches",
                   return_value=False), \
             patch("aemr_bot.handlers.admin_appeal_ops.ack_callback", AsyncMock()) as ack:
            await admin_appeal_ops.run_reply_cancel(event, "stale")
        drop.assert_not_called()
        assert "устарела" in ack.await_args.args[1].lower()


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
             patch("aemr_bot.handlers.admin_appeal_ops.ack_callback", AsyncMock()) as ack:
            await admin_appeal_ops.run_reopen(event, 7)
        write_audit.assert_not_called()
        assert "согласие отозвано" in ack.await_args.args[1]
        event.bot.send_message.assert_not_called()

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
    async def test_stale_close_acknowledges_without_new_card(self) -> None:
        from aemr_bot.handlers import admin_appeal_ops

        event = _make_event()
        with patch("aemr_bot.handlers.admin_appeal_ops.ensure_operator",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_appeal_ops.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_appeal_ops.appeals_service.close",
                   AsyncMock(return_value=False)), \
             patch("aemr_bot.handlers.admin_appeal_ops.ack_callback", AsyncMock()) as ack, \
             patch("aemr_bot.handlers.admin_appeal_ops._show_appeal_card_or_result",
                   AsyncMock()) as show:
            await admin_appeal_ops.run_close(event, 5)
        ack.assert_awaited_once()
        assert "завершено" in ack.await_args.args[1]
        show.assert_not_awaited()

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
    async def test_close_after_prior_reply_records_fact_without_guessing_kind(self) -> None:
        """Старый ответ мог быть и окончательным до возобновления."""
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
        write_audit.assert_awaited_once()
        details = write_audit.call_args.kwargs.get("details")
        assert details == {"after_prior_reply": True}


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


@pytest.mark.asyncio
async def test_reply_button_collects_durable_parts_and_rejects_stale_cancel(session, monkeypatch):
    from datetime import UTC, datetime

    from aemr_bot.db.models import Appeal, Operator, User
    from aemr_bot.handlers import admin_appeal_ops, operator_draft
    from aemr_bot.services import operator_drafts

    user = User(max_user_id=89101, consent_pdn_at=datetime.now(UTC))
    operator = Operator(max_user_id=7, full_name="Специалист", role="aemr")
    session.add_all([user, operator])
    await session.flush()
    appeal = Appeal(user_id=user.id, status="new", summary="Вопрос", attachments=[])
    session.add(appeal)
    await session.commit()
    monkeypatch.setattr(operator_draft, "get_operator", AsyncMock(return_value=operator))
    monkeypatch.setattr(operator_draft, "is_admin_chat", lambda event: True)
    monkeypatch.setattr(operator_draft, "show", AsyncMock())
    ack = AsyncMock()
    monkeypatch.setattr(operator_draft, "ack_callback", ack)
    event = _make_event()
    await admin_appeal_ops.run_reply_intent(event, appeal.id, is_final=False)
    draft = await operator_drafts.get(session, operator.id)
    token = draft.token
    assert draft.appeal_id == appeal.id and draft.is_final is False
    assert await operator_draft.collect(event, event.message.body, "Ответ сохранён")
    # MAX can replay the same input: it remains a single saved part.
    assert await operator_draft.collect(event, event.message.body, "Ответ сохранён")
    draft = await operator_drafts.get(session, operator.id)
    assert draft.data["summary_chunks"] == ["Ответ сохранён"]
    await operator_draft.callback(event, "op:draft:cancel:stale")
    assert "другому черновику" in ack.await_args.args[1]
    assert (await operator_drafts.get(session, operator.id)).token == token
    event.bot.send_message.assert_not_awaited()
    await operator_draft.callback(event, f"op:draft:cancel:{token}")
    assert await operator_drafts.get(session, operator.id) is None
