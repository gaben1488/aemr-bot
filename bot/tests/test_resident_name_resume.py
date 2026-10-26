"""Name repair preserves citizen state and resumes the interrupted action."""
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aemr_bot.db.models import DialogState
from aemr_bot.handlers import appeal, appeal_funnel, appeal_runtime, resident_name_gate, start
from tests._helpers import make_event


@pytest.fixture
def resident(monkeypatch):
    user = SimpleNamespace(id=9, max_user_id=42, first_name=None, phone="+79123456789",
                           consent_pdn_at=datetime.now(UTC), is_blocked=False,
                           dialog_state=DialogState.AWAITING_ADDRESS.value,
                           dialog_data={"locality": "Елизовское ГП", "progress_message_id": "p1"})
    session = SimpleNamespace()

    @asynccontextmanager
    async def current_user(*args, **kwargs):
        yield session, user

    @asynccontextmanager
    async def scope():
        yield session

    async def set_state(_s, _id, state, data=None):
        user.dialog_state = state.value
        if data is not None:
            user.dialog_data = data

    async def set_name(_s, _id, name):
        user.first_name = name

    async def merge(_s, _id, patch):
        user.dialog_data.update(patch(user.dialog_data) if callable(patch) else patch)
        return user.dialog_data

    for module in (resident_name_gate, appeal_funnel, appeal_runtime, start):
        monkeypatch.setattr(module, "current_user", current_user)
    monkeypatch.setattr(appeal_funnel, "session_scope", scope)
    monkeypatch.setattr(resident_name_gate.users_service, "set_state", AsyncMock(side_effect=set_state))
    monkeypatch.setattr(resident_name_gate.users_service, "set_first_name", AsyncMock(side_effect=set_name))
    monkeypatch.setattr(resident_name_gate.users_service, "update_dialog_data", AsyncMock(side_effect=merge))
    monkeypatch.setattr(resident_name_gate, "send_or_edit_screen", AsyncMock())
    return user


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["Иван", "Анна Ивановна Петрова"])
async def test_address_input_survives_name_repair_and_resumes(resident, monkeypatch, text):
    event = make_event(chat_id=100, user_id=42, first_name="МегаБосс")
    original = dict(resident.dialog_data)
    assert await resident_name_gate.require_name(event, 42, text="Ленина 5", body=None)
    assert resident.dialog_state == DialogState.AWAITING_NAME.value
    assert resident.dialog_data[resident_name_gate.RESUME_KEY]["data"] == original
    ask_topic = AsyncMock()
    monkeypatch.setattr(appeal_funnel, "ask_topic", ask_topic)
    await appeal_funnel.on_awaiting_name(event, None, text, 42)
    assert resident.first_name == text
    assert resident.dialog_data["address"] == "Ленина 5"
    assert resident.dialog_data["locality"] == original["locality"]
    ask_topic.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_name_keeps_original_pending_message(resident, monkeypatch):
    event = make_event(chat_id=100, user_id=42, first_name="Иван")
    await resident_name_gate.require_name(event, 42, text="Ленина 5", body=None)
    saved = dict(resident.dialog_data)
    await appeal_funnel.on_awaiting_name(event, None, "МегаБосс", 42)
    assert resident.first_name is None
    assert resident.dialog_data == saved
    assert resident.dialog_state == DialogState.AWAITING_NAME.value


@pytest.mark.asyncio
async def test_callback_repairs_name_and_preserves_existing_appeal_reference(resident, monkeypatch):
    resident.dialog_state = DialogState.IDLE.value
    resident.dialog_data = {"last_seen_appeal_id": 777}
    event = make_event(chat_id=100, user_id=42)
    dispatch = AsyncMock()
    monkeypatch.setattr(appeal, "_route_callback", dispatch)
    assert await resident_name_gate.require_name(event, 42, payload="appeal:followup:777")
    await appeal_funnel.on_awaiting_name(event, None, "Анна", 42)
    dispatch.assert_awaited_once_with(event, 42, "appeal:followup:777")
    assert resident.id == 9
    assert resident.dialog_data == {"last_seen_appeal_id": 777}


@pytest.mark.asyncio
async def test_summary_attachment_replay_once(resident, monkeypatch):
    resident.dialog_state = DialogState.AWAITING_SUMMARY.value
    resident.dialog_data = {"address": "Ленина 5", "topic": "Дороги"}
    attachment = {"type": "image", "payload": {"token": "photo"}}
    event = make_event(chat_id=100, user_id=42)
    await resident_name_gate.require_name(event, 42, text="Яма", body=SimpleNamespace(attachments=[attachment]))
    finalize = AsyncMock()
    monkeypatch.setattr(appeal_funnel, "finalize_appeal", finalize)
    await appeal_funnel.on_awaiting_name(event, None, "Иван", 42)
    assert resident.dialog_data["summary_chunks"] == ["Яма"]
    assert resident.dialog_data["attachments"] == [attachment]
    finalize.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_during_repair_does_not_reset_pending_work(resident, monkeypatch):
    event = make_event(chat_id=100, user_id=42)
    await resident_name_gate.require_name(event, 42, text="Ленина 5", body=None)
    saved = dict(resident.dialog_data)
    monkeypatch.setattr(start, "_ensure_user", AsyncMock(return_value=resident))
    reset = AsyncMock()
    monkeypatch.setattr(start, "_reset_funnel_if_stuck", reset)
    await start.cmd_start(event)
    assert resident.dialog_data == saved
    reset.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("state,consented", [(DialogState.AWAITING_CONTACT, True), (DialogState.AWAITING_CONSENT, False), (DialogState.IDLE, False)])
async def test_normal_new_user_onboarding_is_not_intercepted(resident, state, consented):
    resident.dialog_state = state.value
    resident.consent_pdn_at = datetime.now(UTC) if consented else None
    assert not await resident_name_gate.require_name(make_event(), 42, payload="menu:new_appeal")


@pytest.mark.parametrize("payload", ["cancel", "consent:no", "goodbye:erase_yes", "settings:forget_yes", "info:subscribe_off", "settings:help", "settings:policy"])
def test_safe_exit_callbacks_are_not_gated(payload):
    assert not resident_name_gate.gates_callback(payload)


@pytest.mark.asyncio
async def test_recovery_preserves_ready_summary_until_name(resident, monkeypatch):
    resident.dialog_state = DialogState.AWAITING_SUMMARY.value
    resident.dialog_data = {"summary_chunks": ["Яма"], "address": "Ленина 5", "attachments": []}
    original = dict(resident.dialog_data)
    create = AsyncMock()
    monkeypatch.setattr(appeal_runtime.appeals_service, "create_appeal", create)
    event = make_event(chat_id=100, user_id=42)
    result = await appeal_runtime.persist_and_dispatch_appeal(event.bot, 42, reset_empty=True)
    assert result == appeal_runtime.PERSIST_NAME_REQUIRED
    assert resident.dialog_data[resident_name_gate.RESUME_KEY]["data"] == original
    create.assert_not_awaited()
    finalize = AsyncMock()
    monkeypatch.setattr(appeal_funnel, "finalize_appeal", finalize)
    await appeal_funnel.on_awaiting_name(event, None, "Иван", 42)
    finalize.assert_awaited_once()
    assert resident.dialog_data == original


@pytest.mark.asyncio
async def test_contact_name_same_policy_and_foreign_contact_rejected(resident, monkeypatch):
    resident.dialog_state = DialogState.AWAITING_CONTACT.value
    event = make_event(chat_id=100, user_id=42)
    save_phone = AsyncMock()
    monkeypatch.setattr(appeal_funnel.users_service, "set_phone", save_phone)
    monkeypatch.setattr(appeal_funnel, "extract_resident_contact", lambda *a: (None, None, True))
    await appeal_funnel.on_awaiting_contact(event, None, "+79123456789", 42)
    save_phone.assert_not_awaited()
    assert resident.first_name is None


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["\nИван", "Иван\t", " " * 121 + "Иван"])
async def test_manual_name_validates_raw_input(resident, text):
    resident.dialog_state = DialogState.AWAITING_NAME.value
    await appeal_funnel.on_awaiting_name(make_event(), None, text, 42)
    assert resident.first_name is None


@pytest.mark.asyncio
async def test_crash_after_restore_keeps_input_for_next_entry(resident, monkeypatch):
    event = make_event(chat_id=100, user_id=42)
    await resident_name_gate.require_name(event, 42, text="Ленина 5", body=None)
    replay = resident_name_gate.resume_after_name
    monkeypatch.setattr(resident_name_gate, "resume_after_name", AsyncMock(side_effect=RuntimeError("crash")))
    with pytest.raises(RuntimeError, match="crash"):
        await appeal_funnel.on_awaiting_name(event, None, "Иван", 42)
    assert resident.dialog_data[resident_name_gate.RESUME_KEY]["ready"]
    assert resident.dialog_data[resident_name_gate.RESUME_KEY]["text"] == "Ленина 5"
    monkeypatch.setattr(resident_name_gate, "resume_after_name", replay)
    monkeypatch.setattr(appeal_funnel, "ask_topic", AsyncMock())
    assert await resident_name_gate.require_name(event, 42)
    assert resident.dialog_data["address"] == "Ленина 5"
    assert resident_name_gate.RESUME_KEY not in resident.dialog_data


@pytest.mark.asyncio
async def test_crash_after_summary_merge_does_not_duplicate_content(resident, monkeypatch):
    resident.dialog_state = DialogState.AWAITING_SUMMARY.value
    resident.dialog_data = {"address": "Ленина 5"}
    event = make_event(chat_id=100, user_id=42)
    await resident_name_gate.require_name(event, 42, text="Яма", body=None)
    finalize = AsyncMock(side_effect=RuntimeError("crash"))
    monkeypatch.setattr(appeal_funnel, "finalize_appeal", finalize)
    with pytest.raises(RuntimeError, match="crash"):
        await appeal_funnel.on_awaiting_name(event, None, "Иван", 42)
    assert resident.dialog_data[resident_name_gate.RESUME_KEY]["applied"]
    assert resident.dialog_data["summary_chunks"] == ["Яма"]
    finalize.side_effect = None
    await resident_name_gate.require_name(event, 42)
    assert resident.dialog_data["summary_chunks"] == ["Яма"]
    assert finalize.await_count == 2


@pytest.mark.asyncio
async def test_startup_does_not_reset_input_before_replay(resident, monkeypatch):
    resident.first_name = "Иван"
    resident.dialog_state = DialogState.AWAITING_SUMMARY.value
    resident.dialog_data = {resident_name_gate.RESUME_KEY: {
        "ready": True, "state": DialogState.AWAITING_SUMMARY.value,
        "data": {}, "text": "Яма", "attachments": [],
    }}
    monkeypatch.setattr(appeal_runtime.appeals_service, "count_recent_for_user", AsyncMock(return_value=0))
    reset = AsyncMock()
    monkeypatch.setattr(appeal_runtime.users_service, "reset_state", reset)
    assert await appeal_runtime.persist_and_dispatch_appeal(make_event().bot, 42, reset_empty=True) is None
    reset.assert_not_awaited()
    assert resident.dialog_data[resident_name_gate.RESUME_KEY]["text"] == "Яма"


@pytest.mark.asyncio
async def test_watchdog_preserves_name_repair_draft(session):
    from datetime import timedelta

    from aemr_bot.db.models import User
    from aemr_bot.services import users

    old = datetime.now(UTC) - timedelta(days=3)
    session.add_all([
        User(max_user_id=1, dialog_state=DialogState.AWAITING_NAME.value,
             dialog_data={resident_name_gate.RESUME_KEY: {"state": "awaiting_summary", "data": {}, "text": "Яма"}},
             updated_at=old),
        User(max_user_id=2, dialog_state=DialogState.AWAITING_NAME.value,
             dialog_data={}, updated_at=old),
    ])
    await session.flush()
    stale = await users.find_stuck_in_funnel(session, idle_seconds=3600)
    assert (1, DialogState.AWAITING_NAME.value) not in stale
    assert (2, DialogState.AWAITING_NAME.value) in stale


@pytest.mark.asyncio
async def test_start_with_ready_replay_does_not_reset_resumed_stage(resident, monkeypatch):
    event = make_event(chat_id=100, user_id=42)
    resident.first_name = "Иван"
    resident.dialog_data = {resident_name_gate.RESUME_KEY: {
        "ready": True, "state": DialogState.AWAITING_ADDRESS.value,
        "data": {"locality": "Елизовское ГП"}, "text": "Ленина 5", "attachments": [],
    }, "locality": "Елизовское ГП"}

    async def ask_topic(*a, **kw):
        resident.dialog_state = DialogState.AWAITING_TOPIC.value

    monkeypatch.setattr(appeal_funnel, "ask_topic", ask_topic)
    monkeypatch.setattr(start, "_ensure_user", AsyncMock(return_value=resident))
    reset = AsyncMock()
    monkeypatch.setattr(start, "_reset_funnel_if_stuck", reset)
    await start.cmd_start(event)
    assert resident.dialog_state == DialogState.AWAITING_TOPIC.value
    assert resident.dialog_data["address"] == "Ленина 5"
    reset.assert_not_awaited()


@pytest.mark.asyncio
async def test_replay_consumes_triggering_text_with_explicit_resend_notice(resident, monkeypatch):
    event = make_event(chat_id=100, user_id=42)
    resident.first_name = "Иван"
    resident.dialog_data = {resident_name_gate.RESUME_KEY: {
        "ready": True, "state": DialogState.AWAITING_ADDRESS.value,
        "data": {}, "text": "Ленина 5", "attachments": [],
    }}
    monkeypatch.setattr(appeal_funnel, "ask_topic", AsyncMock())
    assert await resident_name_gate.require_name(event, 42, text="Иван", body=None)
    assert resident.dialog_data["address"] == "Ленина 5"
    assert "ещё раз" in event.message.answer.await_args.args[0]
