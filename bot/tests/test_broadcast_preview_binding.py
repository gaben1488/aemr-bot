from unittest.mock import AsyncMock

import pytest

from aemr_bot.handlers import broadcast_wizard as wizard
from tests._helpers import make_event


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["confirm", "edit", "abort"])
async def test_old_preview_never_changes_current_broadcast(monkeypatch, action):
    state = wizard._WizardState(step="awaiting_confirm", text="Current B")
    state.preview_message_id = "new-B"
    monkeypatch.setattr(wizard, "_wizards", {555: state})
    monkeypatch.setattr(wizard, "get_callback_message_id", lambda event: "old-A")
    monkeypatch.setattr(wizard, "_ensure_role", AsyncMock(return_value=False))
    monkeypatch.setattr(wizard, "op_screen", AsyncMock())
    await getattr(wizard, "_handle_" + action)(make_event(chat_id=123, user_id=555, with_callback=True))
    assert wizard._wizards[555] is state
    assert state.text == "Current B"
    assert state.step == "awaiting_confirm"


def test_late_preview_does_not_bind_to_replacement_draft(monkeypatch):
    from types import SimpleNamespace
    first = wizard._WizardState(step="awaiting_confirm", text="A")
    second = wizard._WizardState(step="awaiting_confirm", text="B")
    monkeypatch.setattr(wizard, "_wizards", {555: second})
    wizard.bind_preview(555, first, SimpleNamespace(message_id="late-A"))
    assert second.preview_message_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mid", [None, "old-A"])
async def test_unbound_or_stale_confirmation_keeps_current_composition(monkeypatch, mid):
    state = wizard._WizardState(step="awaiting_confirm", text="B", attachments=[{"type": "image"}])
    monkeypatch.setattr(wizard, "_wizards", {555: state})
    monkeypatch.setattr(wizard, "get_callback_message_id", lambda event: mid)
    create = AsyncMock()
    monkeypatch.setattr(wizard.broadcasts_service, "create_broadcast", create)
    await wizard._handle_confirm(make_event(chat_id=123, user_id=555, with_callback=True))
    assert wizard._wizards[555] is state
    assert state.attachments == [{"type": "image"}]
    create.assert_not_called()


@pytest.mark.asyncio
async def test_slow_start_never_binds_prompt_to_newer_composer(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from aemr_bot.handlers import operator_input

    replacement = wizard._WizardState(step="awaiting_confirm", text="B", preview_message_id="B-preview")
    @asynccontextmanager
    async def scope():
        yield object()
    async def delayed_read(_):
        wizard._wizards[555] = replacement
        return 3
    monkeypatch.setattr(wizard, "_wizards", {})
    monkeypatch.setattr(wizard, "session_scope", scope)
    monkeypatch.setattr(wizard, "_resolve_broadcast_max_images", delayed_read)
    monkeypatch.setattr(wizard, "_ensure_role", AsyncMock(return_value=True))
    monkeypatch.setattr(operator_input, "pause_reply", AsyncMock())
    monkeypatch.setattr(operator_input, "clear_other_consumers", lambda _: None)
    monkeypatch.setattr(wizard, "op_screen", AsyncMock(return_value=SimpleNamespace(message_id="late-A-prompt")))
    await wizard._start_wizard(make_event(chat_id=123, user_id=555))
    assert wizard._wizards[555] is replacement
    assert replacement.preview_message_id == "B-preview"


@pytest.mark.asyncio
async def test_concurrent_text_cannot_rebind_different_preview(monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    first_sending = asyncio.Event()
    release = asyncio.Event()
    @asynccontextmanager
    async def scope():
        yield object()
    async def answer_a(*args, **kwargs):
        first_sending.set()
        await release.wait()
        return SimpleNamespace(message_id="preview-A")
    monkeypatch.setattr(wizard, "_wizards", {555: wizard._WizardState(step="awaiting_text")})
    monkeypatch.setattr(wizard, "session_scope", scope)
    monkeypatch.setattr(wizard, "_resolve_broadcast_max_images", AsyncMock(return_value=3))
    monkeypatch.setattr(wizard.broadcasts_service, "count_subscribers", AsyncMock(return_value=1))
    monkeypatch.setattr(wizard, "_schedule_persist", lambda *a: None)
    a = make_event(chat_id=123, user_id=555)
    a.message.answer = AsyncMock(side_effect=answer_a)
    task = asyncio.create_task(wizard._handle_wizard_text(a, "A"))
    await first_sending.wait()
    b = make_event(chat_id=123, user_id=555)
    assert not await wizard._handle_wizard_text(b, "B")
    release.set()
    assert await task
    assert wizard._wizards[555].text == "A"
    assert wizard._wizards[555].preview_message_id == "preview-A"
