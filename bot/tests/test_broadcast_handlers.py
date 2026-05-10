"""Тесты для handlers/broadcast — wizard рассылок и helpers.

Локально skip без maxapi; в CI работает.

Покрываем:
- _start_wizard: not-it/coordinator, no user_id, ok (cleanup чужих
  wizard'ов, set state, prompt)
- _handle_wizard_text: no-actor / no-state / wrong-step / expired /
  /cancel / too-long / empty / no-subs / preview-success
- _handle_confirm: no-actor / wrong-step / expired / no-operator /
  no-subscribers / happy path (создаёт broadcast + audit + spawn)
- _handle_abort: drops state and notifies
- _handle_edit: no-state / resets to awaiting_text
- _handle_stop: not-admin / not-operator / flipped / already-done
- _format_progress: с failed_suffix и без
- _send_one: success / exception
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("maxapi", reason="handlers тесты требуют maxapi")


def _make_event(*, user_id: int = 7) -> SimpleNamespace:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return SimpleNamespace(
        bot=bot,
        message=SimpleNamespace(
            answer=AsyncMock(),
            sender=SimpleNamespace(user_id=user_id),
            recipient=SimpleNamespace(chat_id=555),
            body=SimpleNamespace(text="", attachments=[], mid="m-1"),
        ),
        callback=SimpleNamespace(callback_id="cb-1"),
    )


@asynccontextmanager
async def _fake_session_scope():
    yield MagicMock()


@pytest.fixture(autouse=True)
def _clean_wizards():
    from aemr_bot.handlers import broadcast

    broadcast._wizards.clear()
    yield
    broadcast._wizards.clear()


# --- _format_progress ---------------------------------------------------------


class TestFormatProgress:
    def test_no_failed_no_suffix(self) -> None:
        from aemr_bot.handlers.broadcast import _format_progress

        s = _format_progress(broadcast_id=1, total=10, delivered=5, failed=0)
        assert "1" in s
        assert "5" in s

    def test_with_failed_includes_suffix(self) -> None:
        from aemr_bot.handlers.broadcast import _format_progress

        s = _format_progress(broadcast_id=1, total=10, delivered=8, failed=2)
        assert "2" in s


# --- _send_one ----------------------------------------------------------------


class TestSendOne:
    @pytest.mark.asyncio
    async def test_success_returns_none(self) -> None:
        from aemr_bot.handlers.broadcast import _send_one

        bot = MagicMock()
        bot.send_message = AsyncMock()
        result = await _send_one(bot, 42, "привет")
        assert result is None

    @pytest.mark.asyncio
    async def test_exception_returns_truncated_repr(self) -> None:
        from aemr_bot.handlers.broadcast import _send_one

        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=RuntimeError("rate limited"))
        result = await _send_one(bot, 42, "привет")
        assert result is not None
        assert "rate limited" in result


# --- _start_wizard ------------------------------------------------------------


class TestStartWizard:
    @pytest.mark.asyncio
    async def test_not_authorized_returns(self) -> None:
        from aemr_bot.handlers import broadcast

        event = _make_event()
        with patch("aemr_bot.handlers.broadcast._ensure_role",
                   AsyncMock(return_value=False)):
            await broadcast._start_wizard(event)
        # wizard не создан
        assert 7 not in broadcast._wizards

    @pytest.mark.asyncio
    async def test_no_user_id_returns(self) -> None:
        from aemr_bot.handlers import broadcast

        event = SimpleNamespace(
            bot=MagicMock(),
            message=SimpleNamespace(sender=None, answer=AsyncMock()),
        )
        with patch("aemr_bot.handlers.broadcast._ensure_role",
                   AsyncMock(return_value=True)):
            await broadcast._start_wizard(event)

    @pytest.mark.asyncio
    async def test_starts_and_drops_other_wizards(self) -> None:
        """При старте broadcast wizard сбрасываем чужие wizard-ы и
        reply-intent того же оператора (F-003 в operator-аудите)."""
        from aemr_bot.handlers import broadcast

        event = _make_event(user_id=7)
        # admin_commands._op_wizards содержит запись для нашего операторa
        from aemr_bot.handlers import admin_commands as admin_cmd_module
        admin_cmd_module._op_wizards[7] = {"step": "awaiting_id"}
        drop_intent = MagicMock()
        with patch("aemr_bot.handlers.broadcast._ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.operator_reply.drop_reply_intent",
                   drop_intent):
            await broadcast._start_wizard(event)
        # Чужой wizard оператора drop'нут
        assert 7 not in admin_cmd_module._op_wizards
        drop_intent.assert_called_with(7)
        # Наш wizard поднят на awaiting_text
        assert 7 in broadcast._wizards
        assert broadcast._wizards[7].step == "awaiting_text"
        event.message.answer.assert_called_once()


# --- _handle_wizard_text ------------------------------------------------------


class TestHandleWizardText:
    @pytest.mark.asyncio
    async def test_no_actor_returns_false(self) -> None:
        from aemr_bot.handlers import broadcast

        event = SimpleNamespace(
            message=SimpleNamespace(sender=None, answer=AsyncMock()),
        )
        result = await broadcast._handle_wizard_text(event, "x")
        assert result is False

    @pytest.mark.asyncio
    async def test_no_state_returns_false(self) -> None:
        from aemr_bot.handlers import broadcast

        event = _make_event(user_id=7)
        result = await broadcast._handle_wizard_text(event, "x")
        assert result is False

    @pytest.mark.asyncio
    async def test_wrong_step_returns_false(self) -> None:
        from aemr_bot.handlers import broadcast

        event = _make_event(user_id=7)
        broadcast._wizards[7] = broadcast._WizardState(step="awaiting_confirm")
        result = await broadcast._handle_wizard_text(event, "x")
        assert result is False

    @pytest.mark.asyncio
    async def test_expired_drops_and_notifies(self) -> None:
        from aemr_bot.handlers import broadcast

        event = _make_event(user_id=7)
        state = broadcast._WizardState(step="awaiting_text")
        state.expires_at = time.monotonic() - 1  # давно протух
        broadcast._wizards[7] = state
        result = await broadcast._handle_wizard_text(event, "x")
        assert result is True
        assert 7 not in broadcast._wizards
        event.message.answer.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancel_command(self) -> None:
        from aemr_bot.handlers import broadcast

        event = _make_event(user_id=7)
        broadcast._wizards[7] = broadcast._WizardState(step="awaiting_text")
        result = await broadcast._handle_wizard_text(event, "/cancel")
        assert result is True
        assert 7 not in broadcast._wizards

    @pytest.mark.asyncio
    async def test_too_long_kept_in_wizard(self) -> None:
        from aemr_bot.handlers import broadcast
        from aemr_bot.config import settings as cfg

        event = _make_event(user_id=7)
        broadcast._wizards[7] = broadcast._WizardState(step="awaiting_text")
        long_text = "x" * (cfg.broadcast_max_chars + 1)
        result = await broadcast._handle_wizard_text(event, long_text)
        assert result is True
        # state не сменился — оператор может прислать ещё раз короче.
        assert broadcast._wizards[7].step == "awaiting_text"

    @pytest.mark.asyncio
    async def test_empty_text_kept_in_wizard(self) -> None:
        from aemr_bot.handlers import broadcast

        event = _make_event(user_id=7)
        broadcast._wizards[7] = broadcast._WizardState(step="awaiting_text")
        result = await broadcast._handle_wizard_text(event, "   ")
        assert result is True
        assert broadcast._wizards[7].step == "awaiting_text"

    @pytest.mark.asyncio
    async def test_no_subscribers_drops_wizard(self) -> None:
        from aemr_bot.handlers import broadcast

        event = _make_event(user_id=7)
        broadcast._wizards[7] = broadcast._WizardState(step="awaiting_text")
        with patch("aemr_bot.handlers.broadcast.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.broadcast.broadcasts_service.count_subscribers",
                   AsyncMock(return_value=0)):
            result = await broadcast._handle_wizard_text(event, "сообщение")
        assert result is True
        assert 7 not in broadcast._wizards

    @pytest.mark.asyncio
    async def test_success_advances_to_confirm(self) -> None:
        from aemr_bot.handlers import broadcast

        event = _make_event(user_id=7)
        broadcast._wizards[7] = broadcast._WizardState(step="awaiting_text")
        with patch("aemr_bot.handlers.broadcast.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.broadcast.broadcasts_service.count_subscribers",
                   AsyncMock(return_value=42)):
            result = await broadcast._handle_wizard_text(event, "сообщение")
        assert result is True
        assert broadcast._wizards[7].step == "awaiting_confirm"
        assert broadcast._wizards[7].text == "сообщение"


# --- _handle_confirm ----------------------------------------------------------


class TestHandleConfirm:
    @pytest.mark.asyncio
    async def test_no_actor(self) -> None:
        from aemr_bot.handlers import broadcast

        event = SimpleNamespace(
            bot=MagicMock(),
            message=SimpleNamespace(sender=None, answer=AsyncMock()),
        )
        await broadcast._handle_confirm(event)

    @pytest.mark.asyncio
    async def test_wrong_step_acks_with_message(self) -> None:
        from aemr_bot.handlers import broadcast

        event = _make_event(user_id=7)
        # awaiting_text вместо awaiting_confirm
        broadcast._wizards[7] = broadcast._WizardState(step="awaiting_text")
        with patch("aemr_bot.handlers.broadcast.ack_callback",
                   AsyncMock()) as ack:
            await broadcast._handle_confirm(event)
        ack.assert_called_once()
        assert 7 not in broadcast._wizards

    @pytest.mark.asyncio
    async def test_no_operator(self) -> None:
        from aemr_bot.handlers import broadcast

        event = _make_event(user_id=7)
        broadcast._wizards[7] = broadcast._WizardState(step="awaiting_confirm")
        broadcast._wizards[7].text = "hi"
        with patch("aemr_bot.handlers.broadcast.ack_callback", AsyncMock()), \
             patch("aemr_bot.handlers.broadcast._get_operator",
                   AsyncMock(return_value=None)):
            await broadcast._handle_confirm(event)
        # broadcast service не вызван
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_subscribers_aborts(self) -> None:
        from aemr_bot.handlers import broadcast

        event = _make_event(user_id=7)
        broadcast._wizards[7] = broadcast._WizardState(step="awaiting_confirm")
        broadcast._wizards[7].text = "hi"
        op = SimpleNamespace(id=10)
        create_broadcast = AsyncMock()
        with patch("aemr_bot.handlers.broadcast.ack_callback", AsyncMock()), \
             patch("aemr_bot.handlers.broadcast._get_operator",
                   AsyncMock(return_value=op)), \
             patch("aemr_bot.handlers.broadcast.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.broadcast.broadcasts_service.count_subscribers",
                   AsyncMock(return_value=0)), \
             patch("aemr_bot.handlers.broadcast.broadcasts_service.create_broadcast",
                   create_broadcast):
            await broadcast._handle_confirm(event)
        create_broadcast.assert_not_called()

    @pytest.mark.asyncio
    async def test_happy_path_creates_broadcast_and_spawns(self) -> None:
        from aemr_bot.handlers import broadcast

        event = _make_event(user_id=7)
        broadcast._wizards[7] = broadcast._WizardState(step="awaiting_confirm")
        broadcast._wizards[7].text = "ВАЖНО"
        op = SimpleNamespace(id=10)
        broadcast_obj = SimpleNamespace(id=99)

        # Закрываем переданную coroutine, чтобы не было RuntimeWarning
        # «coroutine was never awaited» — реальный spawn_background_task
        # запустил бы её через create_task.
        def _consume(coro, **kwargs):
            coro.close()

        spawn = MagicMock(side_effect=_consume)
        with patch("aemr_bot.handlers.broadcast.ack_callback", AsyncMock()), \
             patch("aemr_bot.handlers.broadcast._get_operator",
                   AsyncMock(return_value=op)), \
             patch("aemr_bot.handlers.broadcast.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.broadcast.broadcasts_service.count_subscribers",
                   AsyncMock(return_value=15)), \
             patch("aemr_bot.handlers.broadcast.broadcasts_service.create_broadcast",
                   AsyncMock(return_value=broadcast_obj)), \
             patch("aemr_bot.handlers.broadcast.operators_service.write_audit",
                   AsyncMock()), \
             patch("aemr_bot.main.spawn_background_task", spawn):
            await broadcast._handle_confirm(event)
        spawn.assert_called_once()
        assert spawn.call_args.kwargs["name"] == "broadcast_99"


# --- _handle_abort ------------------------------------------------------------


class TestHandleAbort:
    @pytest.mark.asyncio
    async def test_drops_wizard_and_notifies(self) -> None:
        from aemr_bot.handlers import broadcast

        event = _make_event(user_id=7)
        broadcast._wizards[7] = broadcast._WizardState(step="awaiting_text")
        with patch("aemr_bot.handlers.broadcast.ack_callback", AsyncMock()):
            await broadcast._handle_abort(event)
        assert 7 not in broadcast._wizards
        event.bot.send_message.assert_called_once()


# --- _handle_edit -------------------------------------------------------------


class TestHandleEdit:
    @pytest.mark.asyncio
    async def test_no_wizard_silently_acks(self) -> None:
        from aemr_bot.handlers import broadcast

        event = _make_event(user_id=7)
        with patch("aemr_bot.handlers.broadcast.ack_callback",
                   AsyncMock()) as ack:
            await broadcast._handle_edit(event)
        ack.assert_called_once()
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_resets_to_awaiting_text(self) -> None:
        from aemr_bot.handlers import broadcast

        event = _make_event(user_id=7)
        state = broadcast._WizardState(step="awaiting_confirm")
        state.text = "уже введённый текст"
        broadcast._wizards[7] = state
        with patch("aemr_bot.handlers.broadcast.ack_callback", AsyncMock()):
            await broadcast._handle_edit(event)
        # text сброшен, шаг = awaiting_text
        assert broadcast._wizards[7].step == "awaiting_text"
        assert broadcast._wizards[7].text == ""
        event.bot.send_message.assert_called_once()


# --- _handle_stop -------------------------------------------------------------


class TestHandleStop:
    @pytest.mark.asyncio
    async def test_not_admin_chat_returns(self) -> None:
        from aemr_bot.handlers import broadcast

        event = _make_event()
        with patch("aemr_bot.handlers.broadcast._is_admin_chat",
                   return_value=False), \
             patch("aemr_bot.handlers.broadcast.ack_callback",
                   AsyncMock()) as ack:
            await broadcast._handle_stop(event, 99)
        ack.assert_called_once()

    @pytest.mark.asyncio
    async def test_not_operator_returns(self) -> None:
        from aemr_bot.handlers import broadcast

        event = _make_event()
        with patch("aemr_bot.handlers.broadcast._is_admin_chat",
                   return_value=True), \
             patch("aemr_bot.handlers.broadcast._ensure_operator",
                   AsyncMock(return_value=False)), \
             patch("aemr_bot.handlers.broadcast.ack_callback", AsyncMock()):
            await broadcast._handle_stop(event, 99)

    @pytest.mark.asyncio
    async def test_flipped(self) -> None:
        from aemr_bot.handlers import broadcast

        event = _make_event()
        ack = AsyncMock()
        with patch("aemr_bot.handlers.broadcast._is_admin_chat",
                   return_value=True), \
             patch("aemr_bot.handlers.broadcast._ensure_operator",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.broadcast.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.broadcast.broadcasts_service.request_cancel",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.broadcast.ack_callback", ack):
            await broadcast._handle_stop(event, 99)
        ack.assert_called_once()
        msg_arg = ack.call_args.args[1]
        assert "Остановлено" in msg_arg

    @pytest.mark.asyncio
    async def test_already_done(self) -> None:
        from aemr_bot.handlers import broadcast

        event = _make_event()
        ack = AsyncMock()
        with patch("aemr_bot.handlers.broadcast._is_admin_chat",
                   return_value=True), \
             patch("aemr_bot.handlers.broadcast._ensure_operator",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.broadcast.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.broadcast.broadcasts_service.request_cancel",
                   AsyncMock(return_value=False)), \
             patch("aemr_bot.handlers.broadcast.ack_callback", ack):
            await broadcast._handle_stop(event, 99)
        msg_arg = ack.call_args.args[1]
        assert "Уже завершено" in msg_arg
