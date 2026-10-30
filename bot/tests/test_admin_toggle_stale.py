from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aemr_bot.handlers import admin_settings_notify


@pytest.mark.asyncio
async def test_old_notification_button_does_not_reverse_new_setting(monkeypatch):
    @asynccontextmanager
    async def scope():
        yield SimpleNamespace(commit=AsyncMock())

    monkeypatch.setattr(admin_settings_notify, "session_scope", scope)
    monkeypatch.setattr(admin_settings_notify.settings_store, "get_versioned", AsyncMock(return_value=(False, 3)))
    save = AsyncMock()
    monkeypatch.setattr(admin_settings_notify.settings_store, "set_value", save)
    monkeypatch.setattr(admin_settings_notify, "_show_notify_card", AsyncMock())
    await admin_settings_notify._toggle_notify(SimpleNamespace(), "admin_notify_pulse", expected=True, revision=1)
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_revision_rejects_stale_snapshot_after_value_changes_back(session):
    from aemr_bot.services import settings_store

    key = "admin_notify_pulse"
    await settings_store.set_value(session, key, True)
    _, original_revision = await settings_store.get_versioned(session, key)
    await settings_store.set_value(session, key, False)
    await settings_store.set_value(session, key, True)
    with pytest.raises(settings_store.SettingConflictError):
        await settings_store.set_value(session, key, False, expected_revision=original_revision)
    current, _ = await settings_store.get_versioned(session, key)
    assert current is True


def test_late_contact_pages_retain_the_original_indexes():
    from aemr_bot.ui.settings_keyboards import op_settings_obj_keyboard

    contacts = [{"name": f"Служба {i}", "phone": "112"} for i in range(25)]
    markup = op_settings_obj_keyboard("emergency_contacts", contacts, version=7, page=2)
    payloads = [button.payload for row in markup.payload.buttons for button in row]
    assert "op:set:obj_view:emergency_contacts:16:v7" in payloads
    assert "op:set:obj_view:emergency_contacts:24:v7" not in payloads
    assert "op:set:obj_page:emergency_contacts:3" in payloads
