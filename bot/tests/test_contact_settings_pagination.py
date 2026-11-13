"""Large saved contact values remain accessible to IT without mutation."""
from unittest.mock import AsyncMock

import pytest
from maxapi.methods.send_message import SendMessage

from aemr_bot.handlers import admin_settings, admin_settings_obj
from aemr_bot.services.settings_store import validate
from tests._helpers import fake_session_scope, make_event


@pytest.mark.asyncio
@pytest.mark.parametrize("key,field", [("emergency_contacts", "name"),
                                      ("transport_dispatcher_contacts", "routes")])
async def test_long_contact_values_have_bounded_list_and_complete_item(monkeypatch, key, field):
    items = [{field: (f"{i:02d}-" + "😀" * 1900), "phone": "112"} for i in range(8)]
    assert validate(key, items)[0]
    monkeypatch.setattr(admin_settings_obj, "session_scope", fake_session_scope)
    versioned = AsyncMock(return_value=(items, 7))
    monkeypatch.setattr(admin_settings_obj.settings_store, "get_versioned", versioned)
    event = make_event(chat_id=123, user_id=42)
    screens = []

    async def display(event, text, keyboard, **kwargs):
        SendMessage(bot=event.bot, chat_id=123, text=text, attachments=[keyboard])
        screens.append((text, keyboard))

    monkeypatch.setattr(admin_settings_obj, "op_screen", display)
    await admin_settings_obj._show_obj_card(event, key)
    assert all(sum(2 if ord(c) > 0xFFFF else 1 for c in t) <= 3999 for t, _ in screens)
    await admin_settings_obj._show_obj_item(event, f"{key}:0:v7")
    while True:
        payloads = [b["payload"] for row in screens[-1][1].model_dump()["payload"]["buttons"] for b in row]
        next_button = next((p for p in payloads if p.startswith("op:set:obj_itempage:") and
                            int(p.rsplit(":", 1)[1]) == len(screens) - 1), None)
        if not next_button:
            break
        await admin_settings._route_set_action(event, 42, next_button.removeprefix("op:set:"))
        assert len(screens) < 100
    assert len(screens) > 2
    pages = [text.rsplit("\n\nСтраница ", 1)[0] for text, _ in screens[1:]]
    assert f"{field == 'name' and 'Название' or 'Маршруты'}: {items[0][field]}" in "".join(pages)
    assert "Телефон: 112" in "".join(pages)
    assert all(sum(2 if ord(c) > 0xFFFF else 1 for c in text) <= 3999 for text, _ in screens)
    assert items[0][field].endswith("😀" * 1900)


@pytest.mark.asyncio
async def test_old_item_page_refreshes_after_concurrent_change(monkeypatch):
    key = "emergency_contacts"
    original = [{"name": "Before" * 800, "phone": "112"}]
    changed = [{"name": "After", "phone": "112"}]
    monkeypatch.setattr(admin_settings_obj, "session_scope", fake_session_scope)
    monkeypatch.setattr(admin_settings_obj.settings_store, "get_versioned",
                        AsyncMock(side_effect=[(original, 7), (changed, 8), (changed, 8)]))
    event = make_event(chat_id=123, user_id=42)
    display = AsyncMock()
    monkeypatch.setattr(admin_settings_obj, "op_screen", display)
    await admin_settings_obj._show_obj_item(event, f"{key}:0:v7")
    await admin_settings._route_set_action(event, 42, f"obj_itempage:{key}:0:v7:1")
    assert "изменён другим оператором" in display.await_args.args[1]
    assert "After" in display.await_args.args[1]
    assert original[0]["name"] == "Before" * 800
