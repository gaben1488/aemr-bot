"""Long configured public contact lists stay readable through MAX screens."""
from unittest.mock import AsyncMock

import pytest
from maxapi.methods.send_message import SendMessage

from aemr_bot.handlers import menu
from aemr_bot.services.settings_store import validate
from tests._helpers import fake_session_scope, make_event


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["emergency", "dispatchers"])
async def test_long_contact_list_can_be_read_page_by_page(monkeypatch, kind):
    key = "emergency_contacts" if kind == "emergency" else "transport_dispatcher_contacts"
    field = "name" if kind == "emergency" else "routes"
    items = [{field: f"{i:03d}-" + "😀" * 100, "phone": "112"} for i in range(45)]
    assert validate(key, items)[0]
    monkeypatch.setattr(menu, "session_scope", fake_session_scope)
    monkeypatch.setattr(menu.settings_store, "get", AsyncMock(return_value=items))
    monkeypatch.setattr(menu, "ack_callback", AsyncMock())
    event = make_event(chat_id=100, user_id=42)
    screens = []

    async def send(**kwargs):
        SendMessage(bot=event.bot, **kwargs)  # real pinned SDK text limit
        screens.append(kwargs)

    event.bot.send_message = AsyncMock(side_effect=send)
    await getattr(menu, f"open_{kind}")(event)
    while True:
        buttons = screens[-1]["attachments"][0].model_dump()["payload"]["buttons"]
        payloads = [button["payload"] for row in buttons for button in row]
        assert "menu:useful_info" in payloads and "menu:main" in payloads
        next_page = next((p for p in payloads if p.startswith(f"info:{kind}:page:") and
                          int(p.rsplit(":", 1)[1]) == len(screens)), None)
        if next_page is None:
            break
        from aemr_bot.handlers import callback_router
        route = callback_router.route_for(next_page)
        assert route.group == callback_router.CallbackGroup.CITIZEN_FLOW
        assert not route.admin_allowed
        assert await menu.handle_callback(event, next_page, max_user_id=None)
        assert len(screens) < 100
    assert len(screens) > 1
    text = "".join(screen["text"] for screen in screens)
    for item in items:
        assert item[field] in text
    assert all(sum(2 if ord(c) > 0xFFFF else 1 for c in screen["text"]) <= 3999 for screen in screens)


@pytest.mark.parametrize("kind,field", [("emergency", "name"), ("dispatchers", "routes")])
def test_one_oversized_contact_can_be_reassembled_without_loss(kind, field):
    from aemr_bot.handlers import useful_contacts

    value = "Start" + "😀" * 5000 + "End"
    body = useful_contacts.render(kind, [{field: value, "phone": "112"}])
    screens = [useful_contacts.screen(kind, body, page)[0] for page in range(100)]
    pages = []
    for screen in screens:
        content, footer = screen.rsplit("\n\nСтраница ", 1)
        number, total = map(int, footer.split("/"))
        if number != len(pages) + 1:
            break  # out-of-range requests clamp to the last page
        pages.append(content)
        if number == total:
            break
    assert len(pages) > 1 and "".join(pages) == body
    assert value in "".join(pages) and "112" in "".join(pages)
