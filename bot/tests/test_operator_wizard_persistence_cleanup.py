"""Abandoned operator registrations must not return after a restart."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from aemr_bot.handlers import admin_appeal_ops, admin_operators, admin_operators_wizard
from aemr_bot.services import wizard_registry
from tests._helpers import fake_session_scope, make_event
from tests.test_appeal_dispatcher import _CapturingDispatcher


@pytest.fixture
def pending_registration(monkeypatch):
    admin_operators_wizard._op_wizards[7] = {
        "step": "awaiting_name", "expires_at": float("inf"),
        "target_id": 42, "role": "it",
    }
    persist = Mock()
    monkeypatch.setattr(wizard_registry, "schedule_persist_op", persist)
    yield persist
    admin_operators_wizard._op_wizards.clear()
    wizard_registry.drop_reply_intent(7)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["expiry", "cancel_button", "cancel_command", "reply", "broadcast"])
async def test_abandoning_registration_enqueues_persistent_delete(action, pending_registration, monkeypatch):
    event = make_event(chat_id=123, user_id=7)
    if action == "expiry":
        admin_operators_wizard._op_wizards[7]["expires_at"] = -1
        assert admin_operators_wizard._op_wizard_get(7) is None
    elif action == "cancel_button":
        monkeypatch.setattr(admin_operators, "ensure_role", AsyncMock(return_value=True))
        monkeypatch.setattr(admin_operators, "op_screen", AsyncMock())
        await admin_operators.run_operators_action(event, "op:opadd:cancel")
    elif action == "cancel_command":
        from aemr_bot.handlers import appeal
        monkeypatch.setattr(appeal, "session_scope", fake_session_scope)
        monkeypatch.setattr("aemr_bot.services.operators.get", AsyncMock(return_value=None))
        monkeypatch.setattr(appeal.cfg, "admin_group_id", 123)
        event.message.body.text = "/cancel"
        dispatcher = _CapturingDispatcher()
        appeal.register(dispatcher)
        await dispatcher.message_handler(event)
    elif action == "reply":
        from aemr_bot.handlers import operator_draft
        monkeypatch.setattr(operator_draft, "is_admin_chat", lambda _: True)
        monkeypatch.setattr(operator_draft, "get_operator", AsyncMock(return_value=SimpleNamespace(id=70, max_user_id=7)))
        monkeypatch.setattr(operator_draft, "session_scope", fake_session_scope)
        monkeypatch.setattr(operator_draft, "show", AsyncMock())
        monkeypatch.setattr(operator_draft, "ack_callback", AsyncMock())
        monkeypatch.setattr(operator_draft.appeals, "get_by_id", AsyncMock(return_value=SimpleNamespace(
            id=12, status="new", user=SimpleNamespace(is_blocked=False, consent_pdn_at=True),
        )))
        monkeypatch.setattr(operator_draft.appeals, "mark_in_progress", AsyncMock())
        monkeypatch.setattr(operator_draft.operator_drafts, "get", AsyncMock(return_value=None))
        monkeypatch.setattr(operator_draft.operator_drafts, "start", AsyncMock(return_value=SimpleNamespace(token="draft")))
        await admin_appeal_ops.run_reply_intent(event, 12)
    else:
        from aemr_bot.handlers import broadcast_wizard
        monkeypatch.setattr(broadcast_wizard, "_ensure_role", AsyncMock(return_value=True))
        monkeypatch.setattr(broadcast_wizard, "session_scope", fake_session_scope)
        monkeypatch.setattr(broadcast_wizard, "_resolve_broadcast_max_images", AsyncMock(return_value=10))
        monkeypatch.setattr(broadcast_wizard, "op_screen", AsyncMock())
        monkeypatch.setattr(broadcast_wizard, "_wizards", {})
        await broadcast_wizard._start_wizard(event)
    assert 7 not in admin_operators_wizard._op_wizards
    pending_registration.assert_called_once_with(7, None)
