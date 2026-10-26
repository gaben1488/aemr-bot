"""Broadcast post-0.2.0 regressions for write-step authorization."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aemr_bot.handlers import broadcast_templates_crud as crud
from aemr_bot.handlers.broadcast_templates_state import _TmplWizardState, _wizards


@pytest.mark.parametrize("step", ["rename_awaiting_name", "clone_awaiting_name"])
async def test_template_text_write_rechecks_revoked_role(monkeypatch, step):
    state = _TmplWizardState(step=step, target_id=12, pending_text="Notice")
    _wizards[7] = state
    monkeypatch.setattr(crud, "ensure_role", AsyncMock(return_value=False))
    rename = AsyncMock()
    create = AsyncMock()
    monkeypatch.setattr(crud.templates_service, "rename", rename)
    monkeypatch.setattr(crud.templates_service, "create_template", create)
    handler = crud._step_rename if step == "rename_awaiting_name" else crud._step_clone_name
    assert await handler(SimpleNamespace(), 7, state, "Name", op_id=None)
    assert 7 not in _wizards
    rename.assert_not_awaited()
    create.assert_not_awaited()


@pytest.mark.parametrize("step", ["new_preview", "edit_preview"])
async def test_expired_template_preview_cannot_write(monkeypatch, step):
    from aemr_bot.handlers import broadcast_templates_wizard as wizard

    _wizards[7] = _TmplWizardState(
        step=step, target_id=12, pending_text="Notice", pending_name="Name", expires_at=0,
    )
    monkeypatch.setattr(wizard, "ensure_role", AsyncMock(return_value=True))
    monkeypatch.setattr(wizard, "get_user_id", lambda event: 7)
    monkeypatch.setattr(wizard, "op_screen", AsyncMock())
    create = AsyncMock()
    update = AsyncMock()
    monkeypatch.setattr(wizard.templates_service, "create_template", create)
    monkeypatch.setattr(wizard.templates_service, "update_text", update)
    if step == "new_preview":
        await wizard._save_new(SimpleNamespace())
    else:
        await wizard._save_edit(SimpleNamespace(), 12)
    assert 7 not in _wizards
    create.assert_not_awaited()
    update.assert_not_awaited()


async def test_template_edit_validation_error_returns_to_text(monkeypatch):
    from aemr_bot.handlers import broadcast_templates_wizard as wizard
    from tests._helpers import fake_session_scope

    state = _TmplWizardState(
        step="edit_preview", target_id=12, pending_text="https://untrusted.example",
    )
    _wizards[7] = state
    monkeypatch.setattr(wizard, "ensure_role", AsyncMock(return_value=True))
    monkeypatch.setattr(wizard, "get_user_id", lambda event: 7)
    monkeypatch.setattr(wizard, "get_operator", AsyncMock(return_value=SimpleNamespace(id=7)))
    monkeypatch.setattr(wizard, "session_scope", fake_session_scope)
    screen = AsyncMock()
    monkeypatch.setattr(wizard, "op_screen", screen)
    monkeypatch.setattr(wizard.templates_service, "update_text", AsyncMock(side_effect=ValueError("URL forbidden")))
    audit = AsyncMock()
    monkeypatch.setattr(wizard.operators_service, "write_audit", audit)
    try:
        await wizard._save_edit(SimpleNamespace(), 12)
        assert state.step == "edit_awaiting_text"
        assert "URL forbidden" in screen.await_args.args[1]
        audit.assert_not_awaited()
    finally:
        _wizards.pop(7, None)


async def test_clone_legacy_invalid_template_reports_validation_error(monkeypatch):
    from tests._helpers import fake_session_scope

    state = _TmplWizardState(step="clone_awaiting_name", pending_text="bad legacy text")
    _wizards[7] = state
    monkeypatch.setattr(crud, "ensure_role", AsyncMock(return_value=True))
    monkeypatch.setattr(crud, "session_scope", fake_session_scope)
    monkeypatch.setattr(crud.templates_service, "create_template", AsyncMock(side_effect=ValueError("URL forbidden")))
    answer = AsyncMock()
    event = SimpleNamespace(message=SimpleNamespace(answer=answer))
    assert await crud._step_clone_name(event, 7, state, "Clone", op_id=7)
    assert 7 not in _wizards
    assert "URL forbidden" in answer.await_args.args[0]
