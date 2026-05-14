"""Тесты handlers/_auth и handlers/broadcast (wizard state).

Локально skip без maxapi; в CI работает.

Покрываем:
- _auth.get_operator: not admin chat, no user_id, valid path
- _auth.ensure_operator
- _auth.ensure_role: no operator, wrong role, allowed role
- broadcast._WizardState.expired() / .renew()
- broadcast._drop_expired_wizards
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tests._helpers import fake_session_scope as _fake_session_scope
from tests._helpers import make_event

pytest.importorskip("maxapi", reason="handlers тесты требуют maxapi")


def _make_event(*, chat_id: int = 555, user_id: int = 7) -> SimpleNamespace:
    # Тонкая обёртка над общей фабрикой (tests/_helpers.py): сохраняет
    # файловые дефолты chat_id/user_id, структуру события держит helper.
    return make_event(chat_id=chat_id, user_id=user_id)


class TestGetOperator:
    @pytest.mark.asyncio
    async def test_returns_none_outside_admin_chat(self) -> None:
        from aemr_bot.handlers import _auth

        event = _make_event(chat_id=999)
        with patch("aemr_bot.handlers._auth.is_admin_chat", return_value=False):
            result = await _auth.get_operator(event)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_without_user_id(self) -> None:
        from aemr_bot.handlers import _auth

        event = SimpleNamespace(
            message=SimpleNamespace(
                sender=None,
                recipient=SimpleNamespace(chat_id=555),
            ),
        )
        with patch("aemr_bot.handlers._auth.is_admin_chat", return_value=True):
            result = await _auth.get_operator(event)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_operator_from_db(self) -> None:
        from aemr_bot.handlers import _auth

        event = _make_event()
        op = SimpleNamespace(role="it", id=1)
        with patch("aemr_bot.handlers._auth.is_admin_chat", return_value=True), \
             patch("aemr_bot.handlers._auth.session_scope", _fake_session_scope), \
             patch("aemr_bot.handlers._auth.operators_service.get",
                   AsyncMock(return_value=op)):
            result = await _auth.get_operator(event)
        assert result is op


class TestEnsureOperator:
    @pytest.mark.asyncio
    async def test_true_when_operator_exists(self) -> None:
        from aemr_bot.handlers import _auth

        event = _make_event()
        with patch("aemr_bot.handlers._auth.get_operator",
                   AsyncMock(return_value=SimpleNamespace(role="it"))):
            assert await _auth.ensure_operator(event) is True

    @pytest.mark.asyncio
    async def test_false_when_no_operator(self) -> None:
        from aemr_bot.handlers import _auth

        event = _make_event()
        with patch("aemr_bot.handlers._auth.get_operator",
                   AsyncMock(return_value=None)):
            assert await _auth.ensure_operator(event) is False


class TestEnsureRole:
    @pytest.mark.asyncio
    async def test_returns_false_when_no_operator(self) -> None:
        from aemr_bot.db.models import OperatorRole
        from aemr_bot.handlers import _auth

        event = _make_event()
        with patch("aemr_bot.handlers._auth.get_operator",
                   AsyncMock(return_value=None)):
            ok = await _auth.ensure_role(event, OperatorRole.IT)
        assert ok is False

    @pytest.mark.asyncio
    async def test_returns_false_when_role_not_allowed(self) -> None:
        from aemr_bot.db.models import OperatorRole
        from aemr_bot.handlers import _auth

        event = _make_event()
        # Оператор с ролью EGP, требуем IT.
        op = SimpleNamespace(role=OperatorRole.EGP.value)
        with patch("aemr_bot.handlers._auth.get_operator",
                   AsyncMock(return_value=op)):
            ok = await _auth.ensure_role(event, OperatorRole.IT)
        assert ok is False
        # Должно отправить отказ через message.answer.
        event.message.answer.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_true_when_role_allowed(self) -> None:
        from aemr_bot.db.models import OperatorRole
        from aemr_bot.handlers import _auth

        event = _make_event()
        op = SimpleNamespace(role=OperatorRole.IT.value)
        with patch("aemr_bot.handlers._auth.get_operator",
                   AsyncMock(return_value=op)):
            ok = await _auth.ensure_role(event, OperatorRole.IT, OperatorRole.COORDINATOR)
        assert ok is True


class TestBroadcastWizardState:
    def test_expired_returns_false_initially(self) -> None:
        from aemr_bot.handlers import broadcast

        st = broadcast._WizardState(step="awaiting_text")
        assert st.expired() is False

    def test_expired_returns_true_after_ttl(self) -> None:
        from aemr_bot.handlers import broadcast

        st = broadcast._WizardState(step="awaiting_text")
        # Сдвигаем expires_at в прошлое.
        st.expires_at = time.monotonic() - 1.0
        assert st.expired() is True

    def test_renew_resets_expiry(self) -> None:
        from aemr_bot.handlers import broadcast

        st = broadcast._WizardState(step="awaiting_text")
        st.expires_at = time.monotonic() - 1.0
        assert st.expired() is True
        st.renew()
        assert st.expired() is False


class TestDropExpiredWizards:
    def test_removes_only_expired(self) -> None:
        from aemr_bot.handlers import broadcast

        broadcast._wizards.clear()
        active = broadcast._WizardState(step="awaiting_text")
        stale = broadcast._WizardState(step="awaiting_text")
        stale.expires_at = time.monotonic() - 1.0
        broadcast._wizards[1] = active
        broadcast._wizards[2] = stale
        broadcast._drop_expired_wizards()
        assert 1 in broadcast._wizards
        assert 2 not in broadcast._wizards


class TestAdminCommandsExports:
    def test_admin_commands_reexports_show_op_menu(self) -> None:
        from aemr_bot.handlers import admin_commands

        # Re-export должен указывать на функцию из admin_panel.
        assert callable(admin_commands.show_op_menu)

    def test_admin_commands_has_register(self) -> None:
        from aemr_bot.handlers import admin_commands

        assert callable(admin_commands.register)

    def test_admin_commands_has_run_stats(self) -> None:
        from aemr_bot.handlers import admin_commands

        assert callable(admin_commands.run_stats)

    def test_admin_commands_has_run_open_tickets(self) -> None:
        from aemr_bot.handlers import admin_commands

        assert callable(admin_commands.run_open_tickets)
