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
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests._helpers import fake_session_scope as _fake_session_scope
from tests._helpers import make_event

pytest.importorskip("maxapi", reason="handlers тесты требуют maxapi")


def _make_event(*, user_id: int = 7) -> SimpleNamespace:
    # Обёртка над tests/_helpers.make_event — chat_id жёстко 555
    # (служебная группа), callback нужен broadcast-handler'ам.
    return make_event(chat_id=555, user_id=user_id, with_callback=True)


def _make_callback_event(*, user_id: int = 7) -> SimpleNamespace:
    event = _make_event(user_id=user_id)
    event.bot.edit_message = AsyncMock()
    event.callback.payload = "broadcast:cancel"
    return event


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
        event.bot.send_message.assert_called_once()
        assert "Введите текст рассылки" in event.bot.send_message.call_args.kwargs["text"]


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
             patch("aemr_bot.handlers.broadcast.spawn_background_task", spawn):
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

    @pytest.mark.asyncio
    async def test_callback_edits_preview_card_instead_of_sending_new(self) -> None:
        from aemr_bot.handlers import broadcast

        event = _make_callback_event(user_id=7)
        broadcast._wizards[7] = broadcast._WizardState(step="awaiting_confirm")
        with patch("aemr_bot.handlers.broadcast.ack_callback", AsyncMock()):
            await broadcast._handle_abort(event)

        assert 7 not in broadcast._wizards
        event.bot.edit_message.assert_called_once()
        assert event.bot.edit_message.call_args.kwargs["message_id"] == "m-1"
        event.bot.send_message.assert_not_called()


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

    @pytest.mark.asyncio
    async def test_callback_edits_preview_back_to_text_prompt(self) -> None:
        from aemr_bot.handlers import broadcast

        event = _make_callback_event(user_id=7)
        state = broadcast._WizardState(step="awaiting_confirm")
        state.text = "старый текст"
        broadcast._wizards[7] = state
        with patch("aemr_bot.handlers.broadcast.ack_callback", AsyncMock()):
            await broadcast._handle_edit(event)

        assert broadcast._wizards[7].step == "awaiting_text"
        event.bot.edit_message.assert_called_once()
        assert event.bot.edit_message.call_args.kwargs["message_id"] == "m-1"
        event.bot.send_message.assert_not_called()


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


class TestRunBroadcastImpl:
    @pytest.mark.asyncio
    async def test_final_status_edits_progress_card_with_admin_back_button(self) -> None:
        from aemr_bot.db.models import BroadcastStatus
        from aemr_bot.handlers import broadcast

        bot = MagicMock()
        bot.edit_message = AsyncMock()
        bot.send_message = AsyncMock()
        mark_finished = AsyncMock()

        with patch("aemr_bot.handlers.broadcast.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.broadcast.broadcasts_service.mark_started",
                   AsyncMock()), \
             patch("aemr_bot.handlers.broadcast.broadcasts_service.list_subscriber_targets",
                   AsyncMock(return_value=[])), \
             patch("aemr_bot.handlers.broadcast.broadcasts_service.mark_finished",
                   mark_finished):
            await broadcast._run_broadcast_impl(
                bot,
                broadcast_id=77,
                text="Текст рассылки",
                total=0,
                admin_mid="m-progress",
            )

        bot.edit_message.assert_called_once()
        kwargs = bot.edit_message.call_args.kwargs
        assert kwargs["message_id"] == "m-progress"
        assert "77" in kwargs["text"]
        assert kwargs["attachments"]
        bot.send_message.assert_not_called()
        assert mark_finished.call_args.kwargs["status"] == BroadcastStatus.DONE


class TestComputeProgressStep:
    """_compute_progress_step — адаптивный шаг прогресс-карточки."""

    def test_short_broadcast_shrinks_step_below_default(self) -> None:
        from aemr_bot.config import settings as cfg
        from aemr_bot.handlers.broadcast import _compute_progress_step

        # 5 получателей × 1 сек → estimated 5 сек → step = 0.5 сек,
        # это меньше дефолтного BROADCAST_PROGRESS_UPDATE_SEC.
        step = _compute_progress_step(total=5, rate_delay=1.0)
        assert step < cfg.broadcast_progress_update_sec
        assert step == pytest.approx(0.5)

    def test_long_broadcast_caps_step_at_default(self) -> None:
        from aemr_bot.config import settings as cfg
        from aemr_bot.handlers.broadcast import _compute_progress_step

        # 10000 получателей → estimated/10 огромен → шаг упирается
        # в дефолтный потолок.
        step = _compute_progress_step(total=10_000, rate_delay=1.0)
        assert step == cfg.broadcast_progress_update_sec

    def test_zero_total_does_not_divide_by_zero(self) -> None:
        from aemr_bot.handlers.broadcast import _compute_progress_step

        step = _compute_progress_step(total=0, rate_delay=1.0)
        assert step >= 0


class TestBuildFinalText:
    """_build_final_text — итоговый текст карточки рассылки."""

    def test_cancelled_uses_cancelled_template(self) -> None:
        from aemr_bot.handlers.broadcast import _build_final_text

        text = _build_final_text(
            broadcast_id=7, total=100, delivered=40, failed=0, cancelled=True
        )
        assert "7" in text
        assert "40" in text

    def test_done_without_failures_has_no_failed_line(self) -> None:
        from aemr_bot import texts
        from aemr_bot.handlers.broadcast import _build_final_text

        text = _build_final_text(
            broadcast_id=7, total=100, delivered=100, failed=0, cancelled=False
        )
        # failed_line пустой — подстрока про сбои не появляется.
        failed_fragment = texts.OP_BROADCAST_FAILED_LINE.format(failed=1)[:10]
        assert failed_fragment not in text

    def test_done_with_failures_includes_failed_count(self) -> None:
        from aemr_bot.handlers.broadcast import _build_final_text

        text = _build_final_text(
            broadcast_id=7, total=100, delivered=97, failed=3, cancelled=False
        )
        assert "3" in text


class TestSendFinalSummary:
    """_send_final_summary — публикация итога: edit карточки либо
    fallback-сообщение."""

    @pytest.mark.asyncio
    async def test_edits_card_when_admin_mid_present(self) -> None:
        from aemr_bot.handlers.broadcast import _send_final_summary

        bot = MagicMock()
        bot.edit_message = AsyncMock()
        bot.send_message = AsyncMock()
        await _send_final_summary(
            bot, broadcast_id=7, total=10, delivered=10,
            failed=0, cancelled=False, admin_mid="m-1",
        )
        bot.edit_message.assert_awaited_once()
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_new_message_when_no_admin_mid(self) -> None:
        from aemr_bot.handlers.broadcast import _send_final_summary

        bot = MagicMock()
        bot.edit_message = AsyncMock()
        bot.send_message = AsyncMock()
        await _send_final_summary(
            bot, broadcast_id=7, total=10, delivered=10,
            failed=0, cancelled=False, admin_mid=None,
        )
        bot.edit_message.assert_not_called()
        bot.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_falls_back_to_send_when_edit_raises(self) -> None:
        from aemr_bot.handlers.broadcast import _send_final_summary

        bot = MagicMock()
        bot.edit_message = AsyncMock(side_effect=RuntimeError("stale mid"))
        bot.send_message = AsyncMock()
        await _send_final_summary(
            bot, broadcast_id=7, total=10, delivered=5,
            failed=0, cancelled=True, admin_mid="m-1",
        )
        bot.edit_message.assert_awaited_once()
        bot.send_message.assert_awaited_once()


class TestRunSendLoop:
    """_run_send_loop — цикл отправки, возвращает (delivered, failed,
    cancelled)."""

    @pytest.mark.asyncio
    async def test_delivers_to_all_targets_and_counts(self) -> None:
        from aemr_bot.handlers import broadcast

        bot = MagicMock()
        bot.edit_message = AsyncMock()
        # (user_db_id, max_user_id) — _send_one вернёт None (успех).
        targets = [(1, 101), (2, 102), (3, 103)]
        with patch("aemr_bot.handlers.broadcast._send_one",
                   AsyncMock(return_value=None)) as send_one, \
             patch("aemr_bot.handlers.broadcast.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.broadcast.broadcasts_service.record_deliveries",
                   AsyncMock()), \
             patch("aemr_bot.handlers.broadcast.broadcasts_service.get_status",
                   AsyncMock(return_value="running")), \
             patch("aemr_bot.handlers.broadcast.broadcasts_service.update_progress",
                   AsyncMock()):
            delivered, failed, cancelled = await broadcast._run_send_loop(
                bot,
                broadcast_id=7,
                body="текст",
                total=3,
                targets=targets,
                admin_mid="m-1",
                rate_delay=0,
                progress_step_sec=0,
            )
        assert (delivered, failed, cancelled) == (3, 0, False)
        assert send_one.await_count == 3

    @pytest.mark.asyncio
    async def test_cancelled_status_breaks_loop_early(self) -> None:
        from aemr_bot.handlers import broadcast
        from aemr_bot.db.models import BroadcastStatus

        bot = MagicMock()
        bot.edit_message = AsyncMock()
        targets = [(1, 101), (2, 102), (3, 103)]
        # get_status сразу отдаёт CANCELLED → цикл рвётся после 1-го.
        with patch("aemr_bot.handlers.broadcast._send_one",
                   AsyncMock(return_value=None)) as send_one, \
             patch("aemr_bot.handlers.broadcast.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.broadcast.broadcasts_service.record_deliveries",
                   AsyncMock()), \
             patch("aemr_bot.handlers.broadcast.broadcasts_service.get_status",
                   AsyncMock(return_value=BroadcastStatus.CANCELLED.value)), \
             patch("aemr_bot.handlers.broadcast.broadcasts_service.update_progress",
                   AsyncMock()):
            delivered, failed, cancelled = await broadcast._run_send_loop(
                bot,
                broadcast_id=7,
                body="текст",
                total=3,
                targets=targets,
                admin_mid="m-1",
                rate_delay=0,
                progress_step_sec=0,
            )
        assert cancelled is True
        # Прервались на первом получателе — остальные не тронуты.
        assert send_one.await_count == 1
        assert delivered == 1


class TestFormatDt:
    def test_none_returns_dash(self) -> None:
        from aemr_bot.handlers.broadcast import _format_dt

        assert _format_dt(None) == "—"

    def test_datetime_in_local_tz(self) -> None:
        from datetime import datetime, timezone

        from aemr_bot.handlers.broadcast import _format_dt

        result = _format_dt(datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc))
        # Камчатка UTC+12: 12:00 UTC → 00:00 → дата +1 день
        assert "11.05.2026" in result or "12.05.2026" in result
        assert ":" in result


class TestListBroadcasts:
    @pytest.mark.asyncio
    async def test_blocked_for_non_role(self) -> None:
        from aemr_bot.handlers import broadcast

        event = _make_event()
        with patch("aemr_bot.handlers.broadcast._ensure_role",
                   AsyncMock(return_value=False)):
            await broadcast._list_broadcasts(event)
        event.message.answer.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_list(self) -> None:
        from aemr_bot.handlers import broadcast

        event = _make_event()
        with patch("aemr_bot.handlers.broadcast._ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.broadcast.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.broadcast.broadcasts_service.list_recent",
                   AsyncMock(return_value=[])):
            await broadcast._list_broadcasts(event)
        event.bot.send_message.assert_called_once()
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "рассылок" in text.lower()

    @pytest.mark.asyncio
    async def test_with_items(self) -> None:
        from datetime import datetime, timezone

        from aemr_bot.handlers import broadcast

        event = _make_event()
        items = [
            SimpleNamespace(
                id=42,
                created_at=datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc),
                status="done",
                delivered_count=100,
                subscriber_count_at_start=120,
            ),
        ]
        with patch("aemr_bot.handlers.broadcast._ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.broadcast.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.broadcast.broadcasts_service.list_recent",
                   AsyncMock(return_value=items)):
            await broadcast._list_broadcasts(event)
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "42" in text
        assert "100" in text
