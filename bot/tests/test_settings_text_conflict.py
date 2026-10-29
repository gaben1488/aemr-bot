"""Concurrent text editing must not silently overwrite a colleague's change."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aemr_bot.handlers import admin_settings_text as editor
from aemr_bot.handlers.admin_settings_shared import _edit_intents
from aemr_bot.services import settings_store


@pytest.mark.asyncio
async def test_edit_uses_the_value_displayed_when_the_edit_began(monkeypatch):
    @asynccontextmanager
    async def scope():
        yield object()

    monkeypatch.setattr(editor, "session_scope", scope)
    monkeypatch.setattr(settings_store, "get_versioned", AsyncMock(return_value=("Текст до изменения", 1)))
    show = AsyncMock()
    monkeypatch.setattr(editor, "op_screen", show)
    try:
        await editor._start_edit_intent(SimpleNamespace(), 999, "welcome_text")
        assert _edit_intents[999]["baseline"] == "Текст до изменения"
        # Flipping pages must not replace the baseline by a newer value.
        settings_store.get_versioned.return_value = ("Текст другого оператора", 2)
        await editor._show_edit_page(SimpleNamespace(), 999, "welcome_text", 0)
        assert _edit_intents[999]["baseline"] == "Текст до изменения"
    finally:
        _edit_intents.pop(999, None)


@pytest.mark.asyncio
async def test_conflicting_edit_is_rejected_without_overwriting(monkeypatch):
    @asynccontextmanager
    async def scope():
        yield object()

    monkeypatch.setattr(editor, "session_scope", scope)
    monkeypatch.setattr(settings_store, "validate", lambda *_: (True, ""))
    monkeypatch.setattr(settings_store, "get_versioned", AsyncMock(return_value=("Новая редакция коллеги", 2)))
    save = AsyncMock(side_effect=settings_store.SettingConflictError("welcome_text"))
    monkeypatch.setattr(settings_store, "set_value", save)
    screen = AsyncMock()
    monkeypatch.setattr(editor, "op_screen", screen)
    monkeypatch.setattr(editor.ops_svc, "write_audit", AsyncMock())
    _edit_intents[999] = {"key": "welcome_text", "kind": "single", "baseline": "Старая редакция", "revision": 1, "expires_at": float("inf")}
    try:
        assert await editor._apply_single_edit(SimpleNamespace(), 999, "welcome_text", "Текст оператора")
        assert save.await_args.kwargs["expected_revision"] == 1
        assert "измен" in screen.await_args.args[1].lower()
        editor.ops_svc.write_audit.assert_not_awaited()
    finally:
        _edit_intents.pop(999, None)
