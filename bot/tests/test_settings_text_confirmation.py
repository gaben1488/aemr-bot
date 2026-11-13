"""Proposed resident text is not live until its own preview is confirmed."""
from unittest.mock import AsyncMock

import pytest

from aemr_bot.handlers import admin_settings as facade
from aemr_bot.handlers import admin_settings_text as editor
from aemr_bot.handlers import admin_settings_text_preview as preview
from aemr_bot.handlers.admin_settings_shared import _edit_intents, _intent_get, _intent_set
from tests._helpers import fake_session_scope, make_event


@pytest.fixture
def flow(monkeypatch):
    _edit_intents.clear()
    monkeypatch.setattr(facade, "ensure_role", AsyncMock(return_value=True))
    monkeypatch.setattr(editor, "session_scope", fake_session_scope)
    monkeypatch.setattr(editor.settings_store, "validate", lambda *_: (True, ""))
    save = AsyncMock()
    monkeypatch.setattr(editor.settings_store, "set_value", save)
    monkeypatch.setattr(editor.ops_svc, "write_audit", AsyncMock())
    monkeypatch.setattr(editor, "_show_text_card", AsyncMock())
    monkeypatch.setattr(preview, "op_screen", AsyncMock())
    _intent_set(42, key="welcome_text", kind="single", baseline="Старый текст", revision=7)
    yield save
    _edit_intents.clear()


async def test_proposed_text_is_not_written_before_explicit_confirmation(flow):
    event = make_event(user_id=42)
    await facade.handle_settings_edit_text(event, "Новая редакция")
    flow.assert_not_awaited()
    assert _intent_get(42)["proposed_text"] == "Новая редакция"


async def stage(value="Новая редакция"):
    event = make_event(user_id=42)
    await facade.handle_settings_edit_text(event, value)
    return event, _intent_get(42)["preview_token"]


async def test_confirm_saves_once_with_displayed_revision_and_audit(flow):
    event, token = await stage()
    await preview.callback(event, 42, "confirm:" + token)
    await preview.callback(event, 42, "confirm:" + token)
    flow.assert_awaited_once()
    assert flow.await_args.args[2] == "Новая редакция"
    assert flow.await_args.kwargs["expected_revision"] == 7
    editor.ops_svc.write_audit.assert_awaited_once()
    assert _intent_get(42) is None


async def test_old_confirmation_and_cancel_cannot_change_new_preview(flow):
    event, old = await stage()
    _, new = await stage("Исправленная редакция")
    await preview.callback(event, 42, "confirm:" + old)
    await preview.callback(event, 42, "cancel:" + old)
    flow.assert_not_awaited()
    assert _intent_get(42)["preview_token"] == new
    await preview.callback(event, 42, "cancel:" + new)
    assert _intent_get(42) is None
    flow.assert_not_awaited()


async def test_paging_keeps_full_proposal_and_confirmation_identity(flow):
    value = "🏠" * 1800 + "КОНЕЦ"
    event, token = await stage(value)
    first = preview.op_screen.await_args.args[1]
    await preview.callback(event, 42, "page1:" + token)
    second = preview.op_screen.await_args.args[1]
    assert "КОНЕЦ" in second
    assert all(len(s.encode("utf-16-le")) // 2 < 4000 for s in (first, second))
    assert _intent_get(42)["proposed_text"] == value
    assert _intent_get(42)["preview_token"] == token
    flow.assert_not_awaited()


async def test_conflict_never_writes_audit_or_rebases(flow):
    event, token = await stage()
    flow.side_effect = editor.settings_store.SettingConflictError("welcome_text")
    await preview.callback(event, 42, "confirm:" + token)
    assert flow.await_args.kwargs["expected_revision"] == 7
    editor.ops_svc.write_audit.assert_not_awaited()
    assert _intent_get(42) is None


async def test_lost_it_role_cannot_confirm(flow, monkeypatch):
    event, token = await stage()
    monkeypatch.setattr(facade, "ensure_role", AsyncMock(return_value=False))
    await facade.run_settings_action(event, "op:set:textpreview:confirm:" + token)
    flow.assert_not_awaited()


async def test_foreign_actor_cannot_confirm(flow):
    event, token = await stage()
    await preview.callback(event, 99, "confirm:" + token)
    flow.assert_not_awaited()
    assert _intent_get(42)["preview_token"] == token


async def test_pending_input_survives_stale_legacy_cancel(flow, monkeypatch):
    event, token = await stage()
    monkeypatch.setattr(facade, "op_screen", AsyncMock())
    await facade._route_set_action(event, 42, "cancel:welcome_text")
    assert _intent_get(42)["preview_token"] == token
    flow.assert_not_awaited()


async def test_invalid_replacement_leaves_previous_proposal(flow, monkeypatch):
    event, token = await stage()
    monkeypatch.setattr(editor.settings_store, "validate", lambda *_: (False, "Ошибка"))
    await facade.handle_settings_edit_text(event, "ошибочный вариант")
    assert _intent_get(42)["preview_token"] == token
    flow.assert_not_awaited()


async def test_save_does_not_drop_newer_edit_started_while_saving(flow):
    event, token = await stage()
    async def replace_intent(*args, **kwargs):
        _intent_set(42, key="policy_url", kind="single", baseline="old", revision=20)
    flow.side_effect = replace_intent
    await preview.callback(event, 42, "confirm:" + token)
    assert flow.await_args.kwargs["expected_revision"] == 7
    assert _intent_get(42)["key"] == "policy_url"


async def test_stale_old_value_navigation_does_not_destroy_proposal(flow, monkeypatch):
    event, token = await stage()
    monkeypatch.setattr(facade, "op_screen", AsyncMock())
    await facade._route_set_action(event, 42, "editview:welcome_text:0")
    assert _intent_get(42)["preview_token"] == token
    flow.assert_not_awaited()
