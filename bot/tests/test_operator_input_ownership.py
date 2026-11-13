"""Resuming a private reply must not feed a different administrative composer."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aemr_bot.handlers import (
    admin_audience,
    admin_operators,
    admin_settings,
    broadcast,
    broadcast_templates,
    operator_draft,
)
from aemr_bot.services import operator_drafts, wizard_registry
from tests._helpers import make_event


@pytest.mark.asyncio
async def test_resume_selects_reply_as_only_text_consumer(monkeypatch):
    actor = 555
    draft = SimpleNamespace(state="paused", token="t", data={}, appeal_id=1, is_final=False)
    @asynccontextmanager
    async def scope():
        yield SimpleNamespace(flush=AsyncMock())
    monkeypatch.setattr(operator_draft, "session_scope", scope)
    monkeypatch.setattr(operator_draft, "get_operator", AsyncMock(return_value=SimpleNamespace(id=1, max_user_id=actor)))
    monkeypatch.setattr(operator_drafts, "get", AsyncMock(return_value=draft))
    monkeypatch.setattr(operator_draft, "show", AsyncMock())
    monkeypatch.setattr(wizard_registry, "schedule_persist_broadcast", lambda *a, **k: None)
    monkeypatch.setattr(wizard_registry, "schedule_persist_op", lambda *a, **k: None)
    monkeypatch.setattr(broadcast, "_wizards", {actor: object()})
    monkeypatch.setattr(broadcast_templates, "_wizards", {actor: object()})
    monkeypatch.setattr(admin_audience, "_search_intents", {actor: {}})
    op_drop = []
    settings_drop = []
    monkeypatch.setattr(admin_operators, "_op_wizard_drop", op_drop.append)
    monkeypatch.setattr(admin_settings, "_intent_drop", settings_drop.append)
    wizard_registry.set_reply_intent(actor, 999, 0)
    await operator_draft.callback(make_event(chat_id=123, user_id=actor, with_callback=True), "op:replydraft:resume:t")
    assert draft.state == "editing"
    assert actor not in broadcast._wizards
    assert actor not in broadcast_templates._wizards
    assert actor not in admin_audience._search_intents
    assert op_drop == settings_drop == [actor]
    assert wizard_registry.get_reply_intent(actor) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("initial,expected", [("editing", "paused"), ("paused", "paused"), ("sending", "sending")])
async def test_other_composer_preserves_reply_and_pauses_only_editing(monkeypatch, initial, expected):
    from aemr_bot.handlers import operator_input
    draft = SimpleNamespace(state=initial, data={"summary_chunks": ["private reply"], "attachments": ["file"]})
    @asynccontextmanager
    async def scope():
        yield object()
    monkeypatch.setattr(operator_input, "session_scope", scope)
    monkeypatch.setattr(operator_input.operators, "get", AsyncMock(return_value=SimpleNamespace(id=1)))
    monkeypatch.setattr(operator_drafts, "get", AsyncMock(return_value=draft))
    await operator_input.pause_reply(555)
    assert draft.state == expected
    assert draft.data == {"summary_chunks": ["private reply"], "attachments": ["file"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["template_new", "template_search", "audience_search", "operator_add"])
async def test_new_composer_pauses_reply_before_accepting_text(monkeypatch, entry):
    from aemr_bot.handlers import (
        admin_operators_wizard,
        broadcast_templates_list,
        broadcast_templates_wizard,
        operator_input,
    )
    draft = SimpleNamespace(state="editing", data={"summary_chunks": ["private"], "attachments": ["file"]})
    @asynccontextmanager
    async def scope():
        yield object()
    monkeypatch.setattr(operator_input, "session_scope", scope)
    monkeypatch.setattr(operator_input.operators, "get", AsyncMock(return_value=SimpleNamespace(id=1)))
    monkeypatch.setattr(operator_drafts, "get", AsyncMock(return_value=draft))
    monkeypatch.setattr(wizard_registry, "schedule_persist_broadcast", lambda *a, **k: None)
    monkeypatch.setattr(wizard_registry, "schedule_persist_op", lambda *a, **k: None)
    for module in (broadcast_templates_wizard, broadcast_templates_list, admin_audience, admin_operators_wizard):
        monkeypatch.setattr(module, "op_screen", AsyncMock())
    for module in (broadcast_templates_wizard, broadcast_templates_list):
        monkeypatch.setattr(module, "ensure_role", AsyncMock(return_value=True))
    event = make_event(chat_id=123, user_id=555)
    if entry == "template_new":
        await broadcast_templates_wizard._start_new(event)
    elif entry == "template_search":
        await broadcast_templates_list._start_search(event)
    elif entry == "audience_search":
        await admin_audience._start_search_intent(event, None)
    else:
        await admin_operators_wizard._start_manual_add(event, 555)
    assert draft.state == "paused"
    assert draft.data == {"summary_chunks": ["private"], "attachments": ["file"]}
    operator_input.clear_other_consumers(555)
