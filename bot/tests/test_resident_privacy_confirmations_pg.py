"""Old destructive prompts cannot authorize actions on a later resident edition."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from aemr_bot.db.models import User
from aemr_bot.handlers import menu
from aemr_bot.services import users
from tests._helpers import make_callback_event, make_event


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["erase", "revoke"])
@pytest.mark.parametrize("newer", ["prompt", "consent", "registration"])
async def test_old_prompt_does_not_mutate_newer_resident_edition(session, monkeypatch, action, newer):
    consent = datetime.now(UTC) - timedelta(days=1)
    resident = User(max_user_id=42, first_name="Анна", consent_pdn_at=consent,
                    dialog_data={"summary_chunks": ["Сохранённый черновик"]})
    session.add(resident)
    await session.commit()
    monkeypatch.setattr(menu.admin_events, "notify_consent_revoked", AsyncMock())
    monkeypatch.setattr(menu.admin_events, "notify_data_erased", AsyncMock())
    ask = menu.ask_goodbye_erase_confirm if action == "erase" else menu.ask_goodbye_revoke_confirm
    prompt = make_event(chat_id=42, user_id=42)
    prompt.bot.send_message = AsyncMock(return_value=SimpleNamespace(body=SimpleNamespace(mid="old-prompt")))
    await ask(prompt)
    if newer == "prompt":
        prompt.bot.send_message.return_value = SimpleNamespace(body=SimpleNamespace(mid="new-prompt"))
        await ask(prompt)
    elif newer == "consent":
        await session.refresh(resident)
        resident.consent_pdn_at = datetime.now(UTC)
        await session.commit()
    else:
        await users.erase_pdn_detailed(session, 42)
        await session.commit()
        session.add(User(max_user_id=42, first_name="Новая запись", consent_pdn_at=datetime.now(UTC)))
        await session.commit()
    callback = make_callback_event(chat_id=42, user_id=42, payload=f"goodbye:{action}_yes")
    callback.message.body.mid = "old-prompt"
    callback.bot.send_message.return_value = SimpleNamespace(body=SimpleNamespace(mid="refusal"))
    assert await menu.handle_callback(callback, f"goodbye:{action}_yes", 42)
    await session.rollback()
    current = await session.scalar(select(User).where(User.max_user_id == 42).execution_options(populate_existing=True))
    assert current is not None and current.consent_pdn_at is not None
    if newer == "registration":
        assert current.first_name == "Новая запись"
    else:
        assert current.dialog_data["summary_chunks"] == ["Сохранённый черновик"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("ask_name", "payload", "action"), [
    ("ask_forget_confirm", "settings:forget_yes", "erase"),
    ("ask_goodbye_erase_confirm", "goodbye:erase_yes", "erase"),
    ("ask_consent_revoke_confirm", "settings:consent_revoke_yes", "revoke"),
    ("ask_goodbye_revoke_confirm", "goodbye:revoke_yes", "revoke"),
])
async def test_matching_prompt_allows_only_its_bound_action(session, monkeypatch, ask_name, payload, action):
    resident = User(max_user_id=42, first_name="Анна", consent_pdn_at=datetime.now(UTC),
                    dialog_data={"summary_chunks": ["Часть черновика"]})
    session.add(resident)
    await session.commit()
    monkeypatch.setattr(menu.admin_events, "notify_consent_revoked", AsyncMock())
    monkeypatch.setattr(menu.admin_events, "notify_data_erased", AsyncMock())
    prompt = make_event(chat_id=42, user_id=42)
    prompt.bot.send_message.return_value = SimpleNamespace(body=SimpleNamespace(mid="current-prompt"))
    await getattr(menu, ask_name)(prompt)
    await session.refresh(resident)
    assert resident.dialog_data["summary_chunks"] == ["Часть черновика"]
    assert resident.dialog_data["_privacy_confirmation"]["mid"] == "current-prompt"
    # A callback for the other destructive action cannot consume this prompt.
    other = "goodbye:revoke_yes" if action == "erase" else "goodbye:erase_yes"
    callback = make_callback_event(chat_id=42, user_id=42)
    callback.message.body.mid = "current-prompt"
    assert await menu.handle_callback(callback, other, 42)
    await session.refresh(resident)
    assert resident.consent_pdn_at is not None and "_privacy_confirmation" in resident.dialog_data
    assert await menu.handle_callback(callback, payload, 42)
    await session.rollback()
    current = await session.scalar(select(User).where(User.max_user_id == 42).execution_options(populate_existing=True))
    if action == "erase":
        assert current is None
    else:
        assert current is not None and current.consent_pdn_at is None
        assert "_privacy_confirmation" not in current.dialog_data


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["erase", "revoke"])
async def test_unknown_replacement_prompt_cannot_reuse_previous_authorization(session, monkeypatch, action):
    resident = User(max_user_id=42, first_name="Анна", consent_pdn_at=datetime.now(UTC))
    session.add(resident)
    await session.commit()
    ask = menu.ask_goodbye_erase_confirm if action == "erase" else menu.ask_goodbye_revoke_confirm
    event = make_event(chat_id=42, user_id=42)
    event.bot.send_message.return_value = SimpleNamespace(body=SimpleNamespace(mid="previous-prompt"))
    await ask(event)
    event.bot.send_message.return_value = None
    await ask(event)
    await session.refresh(resident)
    assert "_privacy_confirmation" not in resident.dialog_data
    callback = make_callback_event(chat_id=42, user_id=42)
    callback.message.body.mid = "previous-prompt"
    assert await menu.handle_callback(callback, f"goodbye:{action}_yes", 42)
    await session.refresh(resident)
    assert resident.consent_pdn_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["back", "text"])
async def test_navigation_or_new_draft_input_cancels_pending_erasure(session, monkeypatch, interruption):
    from aemr_bot.handlers import appeal
    from tests.test_appeal_dispatcher import _CapturingDispatcher

    resident = User(max_user_id=42, first_name="Анна", consent_pdn_at=datetime.now(UTC),
                    dialog_state="awaiting_summary" if interruption == "text" else "idle",
                    dialog_data={"summary_chunks": ["Первая часть"], "draft_token": "draft"})
    session.add(resident)
    await session.commit()
    monkeypatch.setattr(menu.admin_events, "notify_data_erased", AsyncMock())
    prompt = make_event(chat_id=42, user_id=42)
    prompt.bot.send_message.return_value = SimpleNamespace(body=SimpleNamespace(mid="prompt-to-cancel"))
    await menu.ask_goodbye_erase_confirm(prompt)
    if interruption == "back":
        navigation = make_callback_event(chat_id=42, user_id=42)
        await menu.handle_callback(navigation, "menu:main", 42)
    else:
        from maxapi.types import MessageCreated

        from aemr_bot import main

        dispatcher = _CapturingDispatcher()
        appeal.register(dispatcher)
        guarded = SimpleNamespace(handle=dispatcher.message_handler)
        main._install_dispatch_guards(guarded)
        message = MessageCreated.model_validate({
            "update_type": "message_created", "timestamp": 1700000000000,
            "message": {"recipient": {"chat_id": 42, "chat_type": "dialog"},
                        "sender": {"user_id": 42, "first_name": "Анна", "is_bot": False},
                        "body": {"mid": "new-part", "seq": 1, "text": "Новая часть"},
                        "timestamp": 1700000000000},
        })
        message.bot = make_event(chat_id=42, user_id=42).bot
        await guarded.handle(message)
    callback = make_callback_event(chat_id=42, user_id=42)
    callback.message.body.mid = "prompt-to-cancel"
    assert await menu.handle_callback(callback, "goodbye:erase_yes", 42)
    await session.rollback()
    current = await session.scalar(select(User).where(User.max_user_id == 42).execution_options(populate_existing=True))
    assert current is not None and current.consent_pdn_at is not None
    if interruption == "text":
        assert current.dialog_data["summary_chunks"] == ["Первая часть", "Новая часть"]


@pytest.mark.asyncio
async def test_new_confirmation_never_reuses_an_editable_prompt_mid(session):
    from aemr_bot.utils import menu_tracker

    session.add(User(max_user_id=42, first_name="Анна", consent_pdn_at=datetime.now(UTC)))
    await session.commit()
    event = make_callback_event(chat_id=42, user_id=42)
    event.message.body.mid = "old-editable-prompt"
    event.bot.edit_message = AsyncMock()
    event.bot.send_message.return_value = SimpleNamespace(body=SimpleNamespace(mid="new-confirmation"))
    menu_tracker.note_editable_send(42, "old-editable-prompt")
    try:
        await menu.ask_goodbye_revoke_confirm(event)
        event.bot.edit_message.assert_not_awaited()
        event.bot.send_message.assert_awaited_once()
        resident = await session.scalar(select(User).where(User.max_user_id == 42))
        assert resident.dialog_data["_privacy_confirmation"]["mid"] == "new-confirmation"
    finally:
        menu_tracker.clear_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("ingress", ["polling", "webhook", "admin", "anonymous", "throttled"])
async def test_private_command_ingress_invalidates_before_dispatch(session, monkeypatch, ingress):
    from maxapi.types import MessageCreated

    from aemr_bot import main

    session.add(User(max_user_id=42, first_name="Анна", consent_pdn_at=datetime.now(UTC)))
    await session.commit()
    prompt = make_event(chat_id=42, user_id=42)
    prompt.bot.send_message.return_value = SimpleNamespace(body=SimpleNamespace(mid="command-prompt"))
    await menu.ask_goodbye_erase_confirm(prompt)
    monkeypatch.setattr(main.settings, "bot_mode", "webhook" if ingress == "webhook" else "polling")
    monkeypatch.setattr(main, "_throttle_allows_event", lambda event: ingress != "throttled")
    called = []

    async def original(event):
        current = await session.scalar(select(User).where(User.max_user_id == 42).execution_options(populate_existing=True))
        # A real slash command reaches its handler only after the old prompt is cancelled.
        assert ("_privacy_confirmation" in current.dialog_data) == (ingress in {"admin", "anonymous"})
        called.append(event.message.body.text)

    dispatcher = SimpleNamespace(handle=original)
    main._install_dispatch_guards(dispatcher)
    event = MessageCreated.model_validate({
        "update_type": "message_created", "timestamp": 1700000000000,
        "message": {"recipient": {"chat_id": main.settings.admin_group_id if ingress == "admin" else 42,
                                  "chat_type": "chat" if ingress == "admin" else "dialog"},
                    "sender": {"user_id": 42, "first_name": "Анна", "is_bot": False},
                    "body": {"mid": "command", "seq": 1, "text": "/menu"}, "timestamp": 1700000000000},
    })
    if ingress == "anonymous":
        event.message.sender = None
    await dispatcher.handle(event)
    assert called == ([] if ingress == "throttled" else ["/menu"])
    if ingress == "throttled":
        current = await session.scalar(select(User).where(User.max_user_id == 42).execution_options(populate_existing=True))
        assert "_privacy_confirmation" in current.dialog_data
