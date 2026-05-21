"""Тесты handlers/broadcast_templates (PR H).

Покрываем:
- handle_callback: разводка `op:tmpl:*` payload'ов;
- handle_wizard_text: шаги new_awaiting_name → new_awaiting_text;
- prefill_wizard_from_template: apply шаблона ставит state в broadcast.

Полное БД-тестирование сервиса — в test_broadcast_templates_service_pg.
Здесь — UI-логика с моками.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tests._helpers import fake_session_scope as _fake_session_scope
from tests._helpers import make_event

pytest.importorskip("maxapi", reason="handlers тесты требуют maxapi")


def _make_event(*, user_id: int = 7, payload: str = "") -> SimpleNamespace:
    event = make_event(chat_id=555, user_id=user_id, with_callback=True)
    event.callback.payload = payload
    event.message.answer = AsyncMock()
    return event


@pytest.fixture(autouse=True)
def _clean_wizards():
    from aemr_bot.handlers import broadcast
    from aemr_bot.handlers import broadcast_templates as bt
    from aemr_bot.utils import menu_tracker

    broadcast._wizards.clear()
    bt._wizards.clear()
    menu_tracker.clear_all()
    yield
    broadcast._wizards.clear()
    bt._wizards.clear()
    menu_tracker.clear_all()


# --- prefill_wizard_from_template ---------------------------------------


class TestPrefillWizard:
    """`apply шаблона` должен ставить broadcast wizard в шаг awaiting_confirm
    с предзаряженными text и attachments — точно как если бы оператор
    только что ввёл их в /broadcast."""

    def test_state_seeded_to_confirm(self) -> None:
        from aemr_bot.handlers import broadcast

        broadcast.prefill_wizard_from_template(
            actor_id=42, text="hello", attachments=[{"type": "image"}]
        )
        state = broadcast._wizards.get(42)
        assert state is not None
        assert state.step == "awaiting_confirm"
        assert state.text == "hello"
        assert state.attachments == [{"type": "image"}]

    def test_state_replaces_existing(self) -> None:
        """Если у оператора уже был незавершённый wizard — он переписывается."""
        from aemr_bot.handlers import broadcast

        broadcast._wizards[42] = broadcast._WizardState(
            step="awaiting_text", text="old"
        )
        broadcast.prefill_wizard_from_template(
            actor_id=42, text="new", attachments=[]
        )
        state = broadcast._wizards[42]
        assert state.step == "awaiting_confirm"
        assert state.text == "new"


# --- handle_callback dispatch ----------------------------------------


class TestHandleCallback:
    """Маршрутизация `op:tmpl:*` payload'ов в нужные функции handler'а."""

    @pytest.mark.asyncio
    async def test_unknown_prefix_returns_false(self) -> None:
        from aemr_bot.handlers import broadcast_templates as bt

        event = _make_event(payload="op:other:list")
        with patch(
            "aemr_bot.handlers.broadcast_templates.is_admin_chat",
            return_value=True,
        ):
            result = await bt.handle_callback(event, "op:other:list")
        assert result is False

    @pytest.mark.asyncio
    async def test_non_admin_chat_returns_false(self) -> None:
        from aemr_bot.handlers import broadcast_templates as bt

        event = _make_event(payload="op:tmpl:list")
        with patch(
            "aemr_bot.handlers.broadcast_templates.is_admin_chat",
            return_value=False,
        ):
            result = await bt.handle_callback(event, "op:tmpl:list")
        assert result is False

    @pytest.mark.asyncio
    async def test_list_dispatched(self) -> None:
        from aemr_bot.handlers import broadcast_templates as bt

        event = _make_event(payload="op:tmpl:list")
        with (
            patch(
                "aemr_bot.handlers.broadcast_templates.is_admin_chat",
                return_value=True,
            ),
            patch(
                "aemr_bot.handlers.broadcast_templates._list",
                new=AsyncMock(),
            ) as mocked_list,
            patch(
                "aemr_bot.handlers.broadcast_templates.ack_callback",
                new=AsyncMock(),
            ),
        ):
            result = await bt.handle_callback(event, "op:tmpl:list")
        assert result is True
        mocked_list.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_open_dispatched_with_id(self) -> None:
        from aemr_bot.handlers import broadcast_templates as bt

        event = _make_event(payload="op:tmpl:open:42")
        with (
            patch(
                "aemr_bot.handlers.broadcast_templates.is_admin_chat",
                return_value=True,
            ),
            patch(
                "aemr_bot.handlers.broadcast_templates._open",
                new=AsyncMock(),
            ) as mocked,
            patch(
                "aemr_bot.handlers.broadcast_templates.ack_callback",
                new=AsyncMock(),
            ),
        ):
            result = await bt.handle_callback(event, "op:tmpl:open:42")
        assert result is True
        mocked.assert_awaited_once()
        # Второй позиционный аргумент — id
        assert mocked.await_args.args[1] == 42

    @pytest.mark.asyncio
    async def test_apply_dispatched_with_id(self) -> None:
        from aemr_bot.handlers import broadcast_templates as bt

        event = _make_event(payload="op:tmpl:apply:7")
        with (
            patch(
                "aemr_bot.handlers.broadcast_templates.is_admin_chat",
                return_value=True,
            ),
            patch(
                "aemr_bot.handlers.broadcast_templates._apply",
                new=AsyncMock(),
            ) as mocked,
            patch(
                "aemr_bot.handlers.broadcast_templates.ack_callback",
                new=AsyncMock(),
            ),
        ):
            await bt.handle_callback(event, "op:tmpl:apply:7")
        mocked.assert_awaited_once()
        assert mocked.await_args.args[1] == 7

    @pytest.mark.asyncio
    async def test_delete_then_delete_ok_routes(self) -> None:
        from aemr_bot.handlers import broadcast_templates as bt

        event = _make_event(payload="op:tmpl:delete:9")
        with (
            patch(
                "aemr_bot.handlers.broadcast_templates.is_admin_chat",
                return_value=True,
            ),
            patch(
                "aemr_bot.handlers.broadcast_templates._ask_delete",
                new=AsyncMock(),
            ) as ask,
            patch(
                "aemr_bot.handlers.broadcast_templates._do_delete",
                new=AsyncMock(),
            ) as do_,
            patch(
                "aemr_bot.handlers.broadcast_templates.ack_callback",
                new=AsyncMock(),
            ),
        ):
            await bt.handle_callback(event, "op:tmpl:delete:9")
            assert ask.await_count == 1 and do_.await_count == 0
            await bt.handle_callback(event, "op:tmpl:delete_ok:9")
            assert do_.await_count == 1

    @pytest.mark.asyncio
    async def test_bad_id_returns_false(self) -> None:
        """Битый числовой хвост (например, после миграции) — не падать,
        вернуть False, caller сделает fallback."""
        from aemr_bot.handlers import broadcast_templates as bt

        event = _make_event(payload="op:tmpl:open:abc")
        with patch(
            "aemr_bot.handlers.broadcast_templates.is_admin_chat",
            return_value=True,
        ):
            result = await bt.handle_callback(event, "op:tmpl:open:abc")
        assert result is False


# --- handle_wizard_text -----------------------------------------------


class TestWizardText:
    """Шаги создания шаблона: имя → текст."""

    @pytest.mark.asyncio
    async def test_no_wizard_no_consume(self) -> None:
        from aemr_bot.handlers import broadcast_templates as bt

        event = _make_event()
        with patch(
            "aemr_bot.handlers.broadcast_templates.is_admin_chat",
            return_value=True,
        ):
            result = await bt.handle_wizard_text(event, "abc")
        assert result is False

    @pytest.mark.asyncio
    async def test_step_new_name_then_text_prompts(self) -> None:
        """После ввода имени wizard переходит в new_awaiting_text и
        показывает соответствующий prompt."""
        from aemr_bot.handlers import broadcast_templates as bt

        bt._wizards[7] = bt._TmplWizardState(step="new_awaiting_name")
        event = _make_event(user_id=7)
        with (
            patch(
                "aemr_bot.handlers.broadcast_templates.is_admin_chat",
                return_value=True,
            ),
            patch(
                "aemr_bot.handlers.broadcast_templates.get_operator",
                new=AsyncMock(return_value=None),
            ),
        ):
            consumed = await bt.handle_wizard_text(event, "Отключение воды")
        assert consumed is True
        assert bt._wizards[7].step == "new_awaiting_text"
        assert bt._wizards[7].pending_name == "Отключение воды"
        event.message.answer.assert_awaited()  # prompt был

    @pytest.mark.asyncio
    async def test_step_new_name_empty_rejects(self) -> None:
        from aemr_bot.handlers import broadcast_templates as bt

        bt._wizards[7] = bt._TmplWizardState(step="new_awaiting_name")
        event = _make_event(user_id=7)
        with (
            patch(
                "aemr_bot.handlers.broadcast_templates.is_admin_chat",
                return_value=True,
            ),
            patch(
                "aemr_bot.handlers.broadcast_templates.get_operator",
                new=AsyncMock(return_value=None),
            ),
        ):
            consumed = await bt.handle_wizard_text(event, "   ")
        assert consumed is True
        assert bt._wizards[7].step == "new_awaiting_name"  # без перехода

    @pytest.mark.asyncio
    async def test_cancel_command_drops_state(self) -> None:
        from aemr_bot.handlers import broadcast_templates as bt

        bt._wizards[7] = bt._TmplWizardState(step="new_awaiting_name")
        event = _make_event(user_id=7)
        with (
            patch(
                "aemr_bot.handlers.broadcast_templates.is_admin_chat",
                return_value=True,
            ),
            patch(
                "aemr_bot.handlers.broadcast_templates.get_operator",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "aemr_bot.handlers.broadcast_templates.send_or_edit_screen",
                new=AsyncMock(),
            ),
        ):
            consumed = await bt.handle_wizard_text(event, "/cancel")
        assert consumed is True
        assert 7 not in bt._wizards

    @pytest.mark.asyncio
    async def test_expired_wizard_dropped_and_passthrough(self) -> None:
        """Если wizard expired, state дропается, и текст идёт дальше."""
        from aemr_bot.handlers import broadcast_templates as bt

        state = bt._TmplWizardState(step="new_awaiting_name")
        state.expires_at = 0  # давно
        bt._wizards[7] = state
        event = _make_event(user_id=7)
        with patch(
            "aemr_bot.handlers.broadcast_templates.is_admin_chat",
            return_value=True,
        ):
            consumed = await bt.handle_wizard_text(event, "name")
        assert consumed is False
        assert 7 not in bt._wizards

    @pytest.mark.asyncio
    async def test_step_new_text_moves_to_preview(self) -> None:
        """PR template-editor-upgrade: после ввода текста wizard
        переходит в new_preview (не сохраняет сразу), оператор сначала
        видит «как увидит подписчик»."""
        from aemr_bot.handlers import broadcast_templates as bt

        state = bt._TmplWizardState(
            step="new_awaiting_text", pending_name="X"
        )
        bt._wizards[7] = state
        event = _make_event(user_id=7)
        with (
            patch(
                "aemr_bot.handlers.broadcast_templates.is_admin_chat",
                return_value=True,
            ),
            patch(
                "aemr_bot.handlers.broadcast_templates.get_operator",
                new=AsyncMock(return_value=SimpleNamespace(id=99)),
            ),
            patch(
                "aemr_bot.handlers.broadcast_templates.session_scope",
                _fake_session_scope,
            ),
            patch(
                "aemr_bot.handlers.broadcast_templates.broadcast_handler._resolve_broadcast_max_images",
                new=AsyncMock(return_value=5),
            ),
            patch(
                "aemr_bot.handlers.broadcast_templates._image_attachments.image_attachments_from_event",
                return_value=[],
            ),
            patch(
                "aemr_bot.handlers.broadcast_templates._image_attachments.build_outbound_image_attachments",
                return_value=[],
            ),
        ):
            consumed = await bt.handle_wizard_text(event, "Уважаемые жители!")
        assert consumed is True
        # Wizard остался — теперь в шаге preview
        assert 7 in bt._wizards
        assert bt._wizards[7].step == "new_preview"
        assert bt._wizards[7].pending_text == "Уважаемые жители!"
        # answer вызван дважды — header превью + сам текст
        assert event.message.answer.await_count >= 1

    @pytest.mark.asyncio
    async def test_save_new_creates_template(self) -> None:
        """После превью callback op:tmpl:save_new сохраняет шаблон и
        очищает state."""
        from aemr_bot.handlers import broadcast_templates as bt

        bt._wizards[7] = bt._TmplWizardState(
            step="new_preview",
            pending_name="Foo",
            pending_text="Body",
            pending_attachments=[],
        )
        event = _make_event(user_id=7)
        fake_tmpl = SimpleNamespace(id=1, name="Foo")
        with (
            patch(
                "aemr_bot.handlers.broadcast_templates.get_operator",
                new=AsyncMock(return_value=SimpleNamespace(id=99)),
            ),
            patch(
                "aemr_bot.handlers.broadcast_templates.session_scope",
                _fake_session_scope,
            ),
            patch(
                "aemr_bot.handlers.broadcast_templates.templates_service.create_template",
                new=AsyncMock(return_value=fake_tmpl),
            ),
            patch(
                "aemr_bot.handlers.broadcast_templates.operators_service.write_audit",
                new=AsyncMock(),
            ),
            patch(
                "aemr_bot.handlers.broadcast_templates.send_or_edit_screen",
                new=AsyncMock(),
            ),
        ):
            await bt._save_new(event)
        assert 7 not in bt._wizards
