"""Тесты для handlers/admin_operators — wizard добавления оператора
(выделено из admin_commands.py рефакторингом 2026-05-10).

Покрываем:
- _op_wizard_get/set/drop: TTL, обновление
- run_operators_menu: not-it / it
- run_operators_action: start/list/cancel/role:* (valid/invalid/wrong-state)
- handle_operators_wizard_text: id (valid/invalid) / name (short/self/upsert)
"""
from __future__ import annotations

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
    )


@asynccontextmanager
async def _fake_session_scope():
    yield MagicMock()


@pytest.fixture(autouse=True)
def _clean_wizards():
    """Изоляция между тестами — глобальный _op_wizards мог остаться от
    предыдущего теста."""
    from aemr_bot.handlers import admin_operators

    admin_operators._op_wizards.clear()
    yield
    admin_operators._op_wizards.clear()


# --- _op_wizard helpers -------------------------------------------------------


class TestWizardHelpers:
    def test_get_returns_none_when_empty(self) -> None:
        from aemr_bot.handlers import admin_operators

        assert admin_operators._op_wizard_get(1) is None

    def test_set_and_get(self) -> None:
        from aemr_bot.handlers import admin_operators

        admin_operators._op_wizard_set(1, step="awaiting_id")
        state = admin_operators._op_wizard_get(1)
        assert state is not None
        assert state["step"] == "awaiting_id"
        assert "expires_at" in state

    def test_set_updates_existing(self) -> None:
        from aemr_bot.handlers import admin_operators

        admin_operators._op_wizard_set(1, step="awaiting_id", target_id=42)
        admin_operators._op_wizard_set(1, step="awaiting_role")
        state = admin_operators._op_wizard_get(1)
        assert state["step"] == "awaiting_role"
        assert state["target_id"] == 42  # ключ сохранился

    def test_drop_removes(self) -> None:
        from aemr_bot.handlers import admin_operators

        admin_operators._op_wizard_set(1, step="awaiting_id")
        admin_operators._op_wizard_drop(1)
        assert admin_operators._op_wizard_get(1) is None

    def test_get_after_ttl_returns_none(self) -> None:
        """Имитируем истёкший TTL — get() должен очистить и вернуть None."""
        from aemr_bot.handlers import admin_operators

        admin_operators._op_wizards[1] = {
            "step": "awaiting_id",
            "expires_at": -1,  # давно протух
        }
        assert admin_operators._op_wizard_get(1) is None
        assert 1 not in admin_operators._op_wizards


# --- run_operators_menu -------------------------------------------------------


class TestOperatorsMenu:
    @pytest.mark.asyncio
    async def test_not_it_blocked(self) -> None:
        from aemr_bot.handlers import admin_operators

        event = _make_event()
        with patch("aemr_bot.handlers.admin_operators.ensure_role",
                   AsyncMock(return_value=False)):
            await admin_operators.run_operators_menu(event)
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_it_sends_menu(self) -> None:
        from aemr_bot.handlers import admin_operators

        event = _make_event()
        with patch("aemr_bot.handlers.admin_operators.ensure_role",
                   AsyncMock(return_value=True)):
            await admin_operators.run_operators_menu(event)
        event.bot.send_message.assert_called_once()


# --- run_operators_action -----------------------------------------------------


class TestOperatorsAction:
    @pytest.mark.asyncio
    async def test_not_it_blocked(self) -> None:
        from aemr_bot.handlers import admin_operators

        event = _make_event()
        with patch("aemr_bot.handlers.admin_operators.ensure_role",
                   AsyncMock(return_value=False)):
            await admin_operators.run_operators_action(event, "op:opadd:start")
        event.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_sets_wizard_and_prompts(self) -> None:
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        with patch("aemr_bot.handlers.admin_operators.ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_operators.run_operators_action(event, "op:opadd:start")
        state = admin_operators._op_wizard_get(7)
        assert state is not None
        assert state["step"] == "awaiting_id"
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "Шаг 1 из 3" in text

    @pytest.mark.asyncio
    async def test_list_empty(self) -> None:
        from aemr_bot.handlers import admin_operators

        event = _make_event()
        with patch("aemr_bot.handlers.admin_operators.ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_operators.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_operators.operators_service.list_active",
                   AsyncMock(return_value=[])), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_operators.run_operators_action(event, "op:opadd:list")
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "пуст" in text

    @pytest.mark.asyncio
    async def test_list_with_operators(self) -> None:
        from aemr_bot.handlers import admin_operators

        event = _make_event()
        ops = [
            SimpleNamespace(max_user_id=1, role="it", full_name="Иванов И.И."),
            SimpleNamespace(max_user_id=2, role="coordinator", full_name="Петрова А."),
        ]
        with patch("aemr_bot.handlers.admin_operators.ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_operators.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_operators.operators_service.list_active",
                   AsyncMock(return_value=ops)), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_operators.run_operators_action(event, "op:opadd:list")
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "Иванов" in text
        assert "Петрова" in text

    @pytest.mark.asyncio
    async def test_cancel_drops_wizard(self) -> None:
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        admin_operators._op_wizard_set(7, step="awaiting_id")
        with patch("aemr_bot.handlers.admin_operators.ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_operators.run_operators_action(event, "op:opadd:cancel")
        assert admin_operators._op_wizard_get(7) is None

    @pytest.mark.asyncio
    async def test_role_invalid(self) -> None:
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        admin_operators._op_wizard_set(7, step="awaiting_role")
        with patch("aemr_bot.handlers.admin_operators.ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_operators.run_operators_action(
                event, "op:opadd:role:bogus"
            )
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "неизвестна" in text

    @pytest.mark.asyncio
    async def test_role_wrong_state(self) -> None:
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        # Wizard НЕ открыт.
        with patch("aemr_bot.handlers.admin_operators.ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_operators.run_operators_action(event, "op:opadd:role:it")
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "Мастер закрыт" in text

    @pytest.mark.asyncio
    async def test_role_valid_advances_to_name(self) -> None:
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        admin_operators._op_wizard_set(7, step="awaiting_role", target_id=42)
        with patch("aemr_bot.handlers.admin_operators.ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.utils.event.ack_callback", AsyncMock()):
            await admin_operators.run_operators_action(event, "op:opadd:role:it")
        state = admin_operators._op_wizard_get(7)
        assert state["step"] == "awaiting_name"
        assert state["role"] == "it"


# --- handle_operators_wizard_text ---------------------------------------------


class TestHandleWizardText:
    @pytest.mark.asyncio
    async def test_no_operator_returns_false(self) -> None:
        from aemr_bot.handlers import admin_operators

        event = SimpleNamespace(
            bot=MagicMock(),
            message=SimpleNamespace(sender=None),
        )
        result = await admin_operators.handle_operators_wizard_text(event, "test")
        assert result is False

    @pytest.mark.asyncio
    async def test_no_active_wizard_returns_false(self) -> None:
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        result = await admin_operators.handle_operators_wizard_text(event, "test")
        assert result is False

    @pytest.mark.asyncio
    async def test_id_invalid_int_prompts_again(self) -> None:
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        admin_operators._op_wizard_set(7, step="awaiting_id")
        result = await admin_operators.handle_operators_wizard_text(event, "abc")
        assert result is True
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "не число" in text.lower()
        # Шаг не продвинулся
        state = admin_operators._op_wizard_get(7)
        assert state["step"] == "awaiting_id"

    @pytest.mark.asyncio
    async def test_id_valid_advances_to_role(self) -> None:
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        admin_operators._op_wizard_set(7, step="awaiting_id")
        result = await admin_operators.handle_operators_wizard_text(event, "42")
        assert result is True
        state = admin_operators._op_wizard_get(7)
        assert state["step"] == "awaiting_role"
        assert state["target_id"] == 42

    @pytest.mark.asyncio
    async def test_name_too_short_rejected(self) -> None:
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        admin_operators._op_wizard_set(
            7, step="awaiting_name", target_id=42, role="it"
        )
        result = await admin_operators.handle_operators_wizard_text(event, "X")
        assert result is True
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "коротк" in text.lower()
        # Wizard остаётся
        assert admin_operators._op_wizard_get(7) is not None

    @pytest.mark.asyncio
    async def test_name_self_modification_blocked(self) -> None:
        """Свою роль через wizard менять нельзя — иначе `it` мог бы
        случайно понизить себя до viewer и потерять доступ."""
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        admin_operators._op_wizard_set(
            7, step="awaiting_name", target_id=7, role="it"
        )
        result = await admin_operators.handle_operators_wizard_text(
            event, "Сам себя"
        )
        assert result is True
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "Изменить свою" in text
        # Wizard сброшен
        assert admin_operators._op_wizard_get(7) is None

    @pytest.mark.asyncio
    async def test_name_valid_creates_new_operator(self) -> None:
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        admin_operators._op_wizard_set(
            7, step="awaiting_name", target_id=42, role="it"
        )
        upsert = AsyncMock()
        with patch("aemr_bot.handlers.admin_operators.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_operators.operators_service.get",
                   AsyncMock(return_value=None)), \
             patch("aemr_bot.handlers.admin_operators.operators_service.upsert",
                   upsert), \
             patch("aemr_bot.handlers.admin_operators.operators_service.write_audit",
                   AsyncMock()):
            result = await admin_operators.handle_operators_wizard_text(
                event, "Иванова Анна Петровна"
            )
        assert result is True
        upsert.assert_called_once()
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "Добавлено" in text  # новый оператор
        assert admin_operators._op_wizard_get(7) is None  # wizard завершён

    @pytest.mark.asyncio
    async def test_name_valid_updates_existing(self) -> None:
        from aemr_bot.handlers import admin_operators

        event = _make_event(user_id=7)
        admin_operators._op_wizard_set(
            7, step="awaiting_name", target_id=42, role="it"
        )
        existing = SimpleNamespace(id=10)
        with patch("aemr_bot.handlers.admin_operators.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_operators.operators_service.get",
                   AsyncMock(return_value=existing)), \
             patch("aemr_bot.handlers.admin_operators.operators_service.upsert",
                   AsyncMock()), \
             patch("aemr_bot.handlers.admin_operators.operators_service.write_audit",
                   AsyncMock()):
            await admin_operators.handle_operators_wizard_text(
                event, "Иванова Анна"
            )
        text = event.bot.send_message.call_args.kwargs["text"]
        assert "Обновлено" in text
