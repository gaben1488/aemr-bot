"""Characterization-тесты broadcast wizard FSM.

PR 2 day plan 2026-05-28 — safety net до Cluster C wave 2
extraction (вынос wizard FSM из `handlers/broadcast.py` в
`handlers/broadcast_wizard.py`). Этот тестовый файл locks
behavior всех ключевых функций wizard'а — извлечение в wave 2
должно сохранить эти инварианты.

Покрывается:
- `_WizardState` — TTL семантика, renew, defaults.
- `_drop_expired_wizards()` — selective cleanup.
- `_start_wizard()` — role check, isolation чужих wizards.
- `_handle_wizard_text()` — все ветки validation/dispatch.
- `_handle_confirm()` — base cases (success path требует Postgres).
- `_handle_abort()` — clean state.
- `_handle_edit()` — return to awaiting_text + clear attachments.
- `prefill_wizard_from_template()` — public API для template-flow.
"""
from __future__ import annotations

import time as _time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


pytest.importorskip("maxapi", reason="нужен maxapi для broadcast импортов")

from tests._helpers import make_event


# ──────────────────────────────────────────────────────────────────────
# _WizardState — pure dataclass
# ──────────────────────────────────────────────────────────────────────


class TestWizardState:
    """`_WizardState` — TTL-кэш состояния мастера на один операторский
    сеанс. Истечение через `cfg.broadcast_wizard_ttl_sec`."""

    def test_default_step_awaiting_text(self) -> None:
        from aemr_bot.handlers.broadcast import _WizardState

        state = _WizardState(step="awaiting_text")
        assert state.step == "awaiting_text"
        assert state.text == ""
        assert state.attachments == []

    def test_attachments_independent_lists(self) -> None:
        """Default factory не должна шарить одну list между instance'ами."""
        from aemr_bot.handlers.broadcast import _WizardState

        a = _WizardState(step="awaiting_text")
        b = _WizardState(step="awaiting_text")
        a.attachments.append({"x": 1})
        assert b.attachments == []

    def test_expired_returns_false_for_fresh(self) -> None:
        from aemr_bot.handlers.broadcast import _WizardState

        state = _WizardState(step="awaiting_text")
        assert state.expired() is False

    def test_expired_returns_true_for_past_expires_at(self) -> None:
        from aemr_bot.handlers.broadcast import _WizardState

        state = _WizardState(step="awaiting_text")
        state.expires_at = _time.monotonic() - 1.0
        assert state.expired() is True

    def test_renew_bumps_expires_at(self) -> None:
        from aemr_bot.handlers.broadcast import _WizardState

        state = _WizardState(step="awaiting_text")
        state.expires_at = _time.monotonic() - 1.0
        assert state.expired() is True
        state.renew()
        assert state.expired() is False


# ──────────────────────────────────────────────────────────────────────
# _drop_expired_wizards
# ──────────────────────────────────────────────────────────────────────


class TestDropExpiredWizards:
    """`_drop_expired_wizards()` — opportunistic cleanup истёкших.
    Вызывается из `_start_wizard` чтобы dict не разрастался от
    оставленных недозаполненных wizard'ов."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        from aemr_bot.handlers import broadcast as mod
        mod._wizards.clear()
        yield
        mod._wizards.clear()

    def test_empty_is_noop(self) -> None:
        from aemr_bot.handlers.broadcast import _drop_expired_wizards

        _drop_expired_wizards()  # не падает на пустом dict

    def test_expired_removed(self) -> None:
        from aemr_bot.handlers import broadcast as mod
        from aemr_bot.handlers.broadcast import (
            _drop_expired_wizards,
            _WizardState,
        )

        state = _WizardState(step="awaiting_text")
        state.expires_at = _time.monotonic() - 1.0
        mod._wizards[42] = state
        _drop_expired_wizards()
        assert 42 not in mod._wizards

    def test_fresh_kept(self) -> None:
        from aemr_bot.handlers import broadcast as mod
        from aemr_bot.handlers.broadcast import (
            _drop_expired_wizards,
            _WizardState,
        )

        mod._wizards[42] = _WizardState(step="awaiting_text")
        _drop_expired_wizards()
        assert 42 in mod._wizards

    def test_mixed_only_expired_removed(self) -> None:
        from aemr_bot.handlers import broadcast as mod
        from aemr_bot.handlers.broadcast import (
            _drop_expired_wizards,
            _WizardState,
        )

        fresh = _WizardState(step="awaiting_text")
        expired = _WizardState(step="awaiting_text")
        expired.expires_at = _time.monotonic() - 1.0
        mod._wizards[10] = fresh
        mod._wizards[20] = expired
        _drop_expired_wizards()
        assert 10 in mod._wizards
        assert 20 not in mod._wizards


# ──────────────────────────────────────────────────────────────────────
# _start_wizard
# ──────────────────────────────────────────────────────────────────────


class TestStartWizard:
    """`_start_wizard` — точка входа `/broadcast` или кнопки. Гарды:
    role check, isolation чужих wizard'ов того же оператора."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        from aemr_bot.handlers import broadcast as mod
        mod._wizards.clear()
        yield
        mod._wizards.clear()

    @pytest.mark.asyncio
    async def test_non_role_no_op(self) -> None:
        from aemr_bot.handlers import broadcast as mod

        event = make_event(user_id=42)
        with patch.object(mod, "_ensure_role",
                          AsyncMock(return_value=False)):
            await mod._start_wizard(event)
        assert 42 not in mod._wizards

    @pytest.mark.asyncio
    async def test_no_user_id_no_op(self) -> None:
        from aemr_bot.handlers import broadcast as mod

        event = make_event(user_id=42)
        with patch.object(mod, "_ensure_role",
                          AsyncMock(return_value=True)), \
             patch.object(mod, "get_user_id", return_value=None):
            await mod._start_wizard(event)
        assert mod._wizards == {}

    @pytest.mark.asyncio
    async def test_role_passes_state_awaiting_text(self) -> None:
        from aemr_bot.handlers import broadcast as mod

        event = make_event(user_id=42)
        with patch.object(mod, "_ensure_role",
                          AsyncMock(return_value=True)), \
             patch.object(mod, "session_scope") as scope, \
             patch.object(mod, "_resolve_broadcast_max_images",
                          AsyncMock(return_value=5)), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()):
            scope.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            scope.return_value.__aexit__ = AsyncMock(return_value=False)
            await mod._start_wizard(event)
        assert 42 in mod._wizards
        assert mod._wizards[42].step == "awaiting_text"

    @pytest.mark.asyncio
    async def test_clears_alien_wizards_of_same_operator(self) -> None:
        """Если у оператора был активный admin_commands wizard или
        reply_intent — они должны сброситься, иначе текст рассылки
        случайно уйдёт жителю как ответ."""
        from aemr_bot.handlers import admin_commands
        from aemr_bot.handlers import broadcast as mod
        from aemr_bot.handlers import operator_reply

        admin_commands._op_wizards[42] = {"step": "awaiting_id"}
        event = make_event(user_id=42)
        with patch.object(mod, "_ensure_role",
                          AsyncMock(return_value=True)), \
             patch.object(mod, "session_scope") as scope, \
             patch.object(mod, "_resolve_broadcast_max_images",
                          AsyncMock(return_value=5)), \
             patch.object(operator_reply, "drop_reply_intent") as drop, \
             patch.object(mod, "send_or_edit_screen", AsyncMock()):
            scope.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            scope.return_value.__aexit__ = AsyncMock(return_value=False)
            await mod._start_wizard(event)
        assert 42 not in admin_commands._op_wizards
        drop.assert_called_once_with(42)


# ──────────────────────────────────────────────────────────────────────
# _handle_wizard_text
# ──────────────────────────────────────────────────────────────────────


class TestHandleWizardText:
    """`_handle_wizard_text` — все ветки validation/dispatch."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        from aemr_bot.handlers import broadcast as mod
        mod._wizards.clear()
        yield
        mod._wizards.clear()

    @pytest.mark.asyncio
    async def test_no_actor_id_returns_false(self) -> None:
        from aemr_bot.handlers import broadcast as mod

        event = make_event()
        with patch.object(mod, "get_user_id", return_value=None):
            result = await mod._handle_wizard_text(event, "text")
        assert result is False

    @pytest.mark.asyncio
    async def test_no_wizard_returns_false(self) -> None:
        from aemr_bot.handlers import broadcast as mod

        event = make_event(user_id=42)
        result = await mod._handle_wizard_text(event, "text")
        assert result is False

    @pytest.mark.asyncio
    async def test_wrong_step_returns_false(self) -> None:
        """Если wizard в awaiting_confirm — текст НЕ перехватываем
        (это срабатывает только в awaiting_text)."""
        from aemr_bot.handlers import broadcast as mod
        from aemr_bot.handlers.broadcast import _WizardState

        event = make_event(user_id=42)
        mod._wizards[42] = _WizardState(step="awaiting_confirm")
        result = await mod._handle_wizard_text(event, "text")
        assert result is False

    @pytest.mark.asyncio
    async def test_expired_wiped_and_message(self) -> None:
        from aemr_bot.handlers import broadcast as mod
        from aemr_bot.handlers.broadcast import _WizardState

        event = make_event(user_id=42)
        state = _WizardState(step="awaiting_text")
        state.expires_at = _time.monotonic() - 1.0
        mod._wizards[42] = state
        result = await mod._handle_wizard_text(event, "any text")
        assert result is True
        assert 42 not in mod._wizards
        event.message.answer.assert_awaited()

    @pytest.mark.asyncio
    async def test_cancel_command_drops_wizard(self) -> None:
        from aemr_bot.handlers import broadcast as mod
        from aemr_bot.handlers.broadcast import _WizardState

        event = make_event(user_id=42)
        mod._wizards[42] = _WizardState(step="awaiting_text")
        result = await mod._handle_wizard_text(event, "/cancel")
        assert result is True
        assert 42 not in mod._wizards

    @pytest.mark.asyncio
    async def test_too_long_text_rejected_state_intact(self) -> None:
        from aemr_bot.handlers import broadcast as mod
        from aemr_bot.handlers.broadcast import _WizardState
        from aemr_bot.config import settings as cfg

        event = make_event(user_id=42)
        mod._wizards[42] = _WizardState(step="awaiting_text")
        long_text = "x" * (cfg.broadcast_max_chars + 100)
        result = await mod._handle_wizard_text(event, long_text)
        assert result is True
        # State остался, step без изменений — оператор может прислать
        # сокращённый текст ещё раз.
        assert 42 in mod._wizards
        assert mod._wizards[42].step == "awaiting_text"

    @pytest.mark.asyncio
    async def test_non_whitelisted_url_rejected(self) -> None:
        from aemr_bot.handlers import broadcast as mod
        from aemr_bot.handlers.broadcast import _WizardState

        event = make_event(user_id=42)
        mod._wizards[42] = _WizardState(step="awaiting_text")
        with patch.object(mod.settings_store, "find_non_whitelisted_urls",
                          return_value=["http://evil.example"]):
            result = await mod._handle_wizard_text(
                event, "Текст с http://evil.example",
            )
        assert result is True
        # State не меняется — оператор пересылает чистый текст.
        assert mod._wizards[42].step == "awaiting_text"
        kwargs = event.message.answer.await_args
        assert kwargs is not None
        # Сообщение содержит «сторонние сайты».
        text_arg = kwargs.args[0] if kwargs.args else kwargs.kwargs.get("text", "")
        assert "сторонние" in text_arg.lower()

    @pytest.mark.asyncio
    async def test_no_subscribers_closes_wizard(self) -> None:
        from aemr_bot.handlers import broadcast as mod
        from aemr_bot.handlers.broadcast import _WizardState

        event = make_event(user_id=42)
        mod._wizards[42] = _WizardState(step="awaiting_text")
        with patch.object(mod.settings_store, "find_non_whitelisted_urls",
                          return_value=[]), \
             patch.object(mod, "session_scope") as scope, \
             patch.object(mod, "_resolve_broadcast_max_images",
                          AsyncMock(return_value=5)), \
             patch.object(mod.broadcasts_service, "count_subscribers",
                          AsyncMock(return_value=0)):
            scope.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            scope.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await mod._handle_wizard_text(
                event, "Объявление: завтра отключение воды.",
            )
        assert result is True
        # Wizard закрыт — рассылать некому, незачем держать state.
        assert 42 not in mod._wizards

    @pytest.mark.asyncio
    async def test_happy_path_advances_to_awaiting_confirm(self) -> None:
        from aemr_bot.handlers import broadcast as mod
        from aemr_bot.handlers.broadcast import _WizardState

        event = make_event(user_id=42)
        mod._wizards[42] = _WizardState(step="awaiting_text")
        with patch.object(mod.settings_store, "find_non_whitelisted_urls",
                          return_value=[]), \
             patch.object(mod, "session_scope") as scope, \
             patch.object(mod, "_resolve_broadcast_max_images",
                          AsyncMock(return_value=5)), \
             patch.object(mod.broadcasts_service, "count_subscribers",
                          AsyncMock(return_value=120)), \
             patch.object(mod._image_attachments,
                          "image_attachments_from_event",
                          return_value=[]), \
             patch.object(mod._image_attachments,
                          "build_outbound_image_attachments",
                          return_value=[]):
            scope.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            scope.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await mod._handle_wizard_text(
                event, "Объявление: завтра отключение воды.",
            )
        assert result is True
        # State перешёл в awaiting_confirm с введённым текстом.
        assert 42 in mod._wizards
        assert mod._wizards[42].step == "awaiting_confirm"
        assert "отключение воды" in mod._wizards[42].text


# ──────────────────────────────────────────────────────────────────────
# _handle_confirm / _handle_abort / _handle_edit
# ──────────────────────────────────────────────────────────────────────


class TestHandleConfirmBase:
    """Базовые гарды `_handle_confirm` (full happy path требует Postgres
    + scheduler — это интеграция, не characterization)."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        from aemr_bot.handlers import broadcast as mod
        mod._wizards.clear()
        yield
        mod._wizards.clear()

    @pytest.mark.asyncio
    async def test_no_actor_id_no_op(self) -> None:
        from aemr_bot.handlers import broadcast as mod

        event = make_event(user_id=42)
        with patch.object(mod, "get_user_id", return_value=None):
            await mod._handle_confirm(event)
        # Ничего критичного не упало.

    @pytest.mark.asyncio
    async def test_no_wizard_acks_closed(self) -> None:
        from aemr_bot.handlers import broadcast as mod

        event = make_event(user_id=42)
        with patch.object(mod, "ack_callback", AsyncMock()) as ack:
            await mod._handle_confirm(event)
        ack.assert_awaited_once()
        # Сообщение «Мастер закрыт.»
        args = ack.await_args.args
        assert "Мастер закрыт" in args[1] if len(args) > 1 else ""

    @pytest.mark.asyncio
    async def test_wrong_step_acks_closed(self) -> None:
        from aemr_bot.handlers import broadcast as mod
        from aemr_bot.handlers.broadcast import _WizardState

        event = make_event(user_id=42)
        # State не в awaiting_confirm.
        mod._wizards[42] = _WizardState(step="awaiting_text")
        with patch.object(mod, "ack_callback", AsyncMock()) as ack:
            await mod._handle_confirm(event)
        # Pop'нули.
        assert 42 not in mod._wizards
        ack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_expired_state_acks_closed(self) -> None:
        from aemr_bot.handlers import broadcast as mod
        from aemr_bot.handlers.broadcast import _WizardState

        event = make_event(user_id=42)
        state = _WizardState(step="awaiting_confirm")
        state.expires_at = _time.monotonic() - 1.0
        mod._wizards[42] = state
        with patch.object(mod, "ack_callback", AsyncMock()) as ack:
            await mod._handle_confirm(event)
        assert 42 not in mod._wizards
        ack.assert_awaited_once()


class TestHandleAbort:
    @pytest.fixture(autouse=True)
    def _clean(self):
        from aemr_bot.handlers import broadcast as mod
        mod._wizards.clear()
        yield
        mod._wizards.clear()

    @pytest.mark.asyncio
    async def test_pops_wizard_and_acks(self) -> None:
        from aemr_bot.handlers import broadcast as mod
        from aemr_bot.handlers.broadcast import _WizardState

        event = make_event(user_id=42)
        mod._wizards[42] = _WizardState(step="awaiting_text")
        with patch.object(mod, "ack_callback", AsyncMock()) as ack, \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send:
            await mod._handle_abort(event)
        assert 42 not in mod._wizards
        ack.assert_awaited_once()
        send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_actor_id_still_acks(self) -> None:
        from aemr_bot.handlers import broadcast as mod

        event = make_event(user_id=42)
        with patch.object(mod, "get_user_id", return_value=None), \
             patch.object(mod, "ack_callback", AsyncMock()) as ack, \
             patch.object(mod, "send_or_edit_screen", AsyncMock()):
            await mod._handle_abort(event)
        ack.assert_awaited_once()


class TestHandleEdit:
    """`_handle_edit` — кнопка «✏️ Изменить текст» в превью. Возвращает
    мастер в awaiting_text и **обнуляет** attachments — чтобы прошлые
    картинки не всплывали поверх нового текста."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        from aemr_bot.handlers import broadcast as mod
        mod._wizards.clear()
        yield
        mod._wizards.clear()

    @pytest.mark.asyncio
    async def test_no_wizard_acks_closed(self) -> None:
        from aemr_bot.handlers import broadcast as mod

        event = make_event(user_id=42)
        with patch.object(mod, "ack_callback", AsyncMock()) as ack:
            await mod._handle_edit(event)
        ack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_resets_to_awaiting_text_clears_attachments(self) -> None:
        from aemr_bot.handlers import broadcast as mod
        from aemr_bot.handlers.broadcast import _WizardState

        event = make_event(user_id=42)
        state = _WizardState(
            step="awaiting_confirm",
            text="Previous text",
            attachments=[{"image": "x"}, {"image": "y"}],
        )
        mod._wizards[42] = state
        with patch.object(mod, "ack_callback", AsyncMock()), \
             patch.object(mod, "send_or_edit_screen", AsyncMock()):
            await mod._handle_edit(event)
        assert mod._wizards[42].step == "awaiting_text"
        assert mod._wizards[42].text == ""
        assert mod._wizards[42].attachments == []


# ──────────────────────────────────────────────────────────────────────
# prefill_wizard_from_template
# ──────────────────────────────────────────────────────────────────────


class TestPrefillWizardFromTemplate:
    """Public API для template-flow: открывает wizard сразу в
    awaiting_confirm с готовым текстом и attachments."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        from aemr_bot.handlers import broadcast as mod
        mod._wizards.clear()
        yield
        mod._wizards.clear()

    def test_creates_state_in_awaiting_confirm(self) -> None:
        from aemr_bot.handlers import broadcast as mod
        from aemr_bot.handlers.broadcast import prefill_wizard_from_template

        prefill_wizard_from_template(
            42, text="Шаблонный текст", attachments=[{"image": "a"}],
        )
        assert 42 in mod._wizards
        state = mod._wizards[42]
        assert state.step == "awaiting_confirm"
        assert state.text == "Шаблонный текст"
        assert state.attachments == [{"image": "a"}]

    def test_overwrites_previous_state(self) -> None:
        """Если у оператора был активный wizard в awaiting_text —
        template-prefill полностью заменяет state, не сохраняет ввод."""
        from aemr_bot.handlers import broadcast as mod
        from aemr_bot.handlers.broadcast import (
            _WizardState,
            prefill_wizard_from_template,
        )

        mod._wizards[42] = _WizardState(
            step="awaiting_text", text="Old draft",
        )
        prefill_wizard_from_template(
            42, text="New from template", attachments=[],
        )
        assert mod._wizards[42].step == "awaiting_confirm"
        assert mod._wizards[42].text == "New from template"

    def test_attachments_list_copy_not_reference(self) -> None:
        """Изменение исходной list attachments не должно мутировать
        state (`list(attachments)` создаёт копию)."""
        from aemr_bot.handlers import broadcast as mod
        from aemr_bot.handlers.broadcast import prefill_wizard_from_template

        src = [{"image": "a"}]
        prefill_wizard_from_template(42, text="x", attachments=src)
        src.append({"image": "b"})
        assert mod._wizards[42].attachments == [{"image": "a"}]
