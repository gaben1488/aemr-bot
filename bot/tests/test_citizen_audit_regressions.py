"""Regression probes from the post-0.2.0 citizen call-path audit."""
import math
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

from aemr_bot.db.models import DialogState
from aemr_bot.handlers import appeal_funnel
from aemr_bot.services import geo
from tests._helpers import fake_session_scope, make_event


@pytest.mark.parametrize("direction", [-1, 1])
@pytest.mark.parametrize("kind,radius", [("building", 100), ("street", 200)])
def test_geo_east_west_candidates_within_radius(direction, kind, radius):
    lat, lon = 53.0, 158.0
    delta = direction * radius * .8 / (111_000 * math.cos(math.radians(lat)))
    point = Point(lon + delta, lat)
    building = (STRtree([point]), [{"street": "Тестовая", "housenumber": "7"}])
    street = (STRtree([LineString([(point.x, lat - .001), (point.x, lat + .001)])]),
              [{"name": "Тестовая"}])
    with patch.object(geo, "find_locality", return_value="Елизовское ГП"), \
         patch.object(geo, "_load_buildings_index", return_value=building if kind == "building" else (None, [])), \
         patch.object(geo, "_load_streets_index", return_value=street if kind == "street" else (None, [])):
        found = geo.find_address(lat, lon)
    assert found.street == "Тестовая"
    assert found.confidence == ("high" if kind == "building" else "medium")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["closed", "answered", "blocked", "revoked", "erased", "cancelled", "reassigned", "ok"])
async def test_followup_rechecks_at_insert_boundary(change):
    initial_user = SimpleNamespace(id=1, consent_pdn_at=datetime.now(UTC), dialog_data={"appeal_id": 5})
    initial_appeal = SimpleNamespace(id=5, user_id=1, status="new")
    fresh_user = SimpleNamespace(id=1, consent_pdn_at=datetime.now(UTC), is_blocked=False,
                                 dialog_state=DialogState.AWAITING_FOLLOWUP_TEXT.value,
                                 dialog_data={"appeal_id": 5})
    fresh_appeal = SimpleNamespace(id=5, user_id=1, status="new", user=fresh_user)
    if change in {"closed", "answered"}:
        fresh_appeal.status = change
    elif change == "blocked":
        fresh_user.is_blocked = True
    elif change == "revoked":
        fresh_user.consent_pdn_at = None
    elif change == "erased":
        fresh_user = None
    elif change == "cancelled":
        fresh_user.dialog_state = DialogState.IDLE.value
    elif change == "reassigned":
        fresh_appeal.user_id = 2
    event = make_event(chat_id=100, user_id=42)
    add_message = AsyncMock()
    get_appeal = AsyncMock(side_effect=[initial_appeal, fresh_appeal])
    get_user = AsyncMock(return_value=fresh_user)
    with patch.object(appeal_funnel, "session_scope", fake_session_scope), \
         patch.object(appeal_funnel.users_service, "get_or_create", AsyncMock(return_value=initial_user)), \
         patch.object(appeal_funnel.users_service, "find_by_max_id", get_user), \
         patch.object(appeal_funnel.users_service, "reset_state", AsyncMock()), \
         patch.object(appeal_funnel.appeals_service, "get_by_id", get_appeal), \
         patch.object(appeal_funnel.appeals_service, "get_by_id_with_messages", AsyncMock(return_value=fresh_appeal)), \
         patch.object(appeal_funnel.appeals_service, "followup_rate_limit_stats", AsyncMock(return_value=(0, None))), \
         patch.object(appeal_funnel.appeals_service, "add_user_message", add_message), \
         patch.object(appeal_funnel.cfg, "admin_group_id", None):
        await appeal_funnel.on_awaiting_followup_text(event, SimpleNamespace(attachments=[]), "Детали", 42)
    assert get_appeal.await_args.kwargs == {"for_update": True}
    assert get_user.await_args.kwargs == {"for_update": True}
    if change == "ok":
        add_message.assert_awaited_once()
    else:
        add_message.assert_not_awaited()
        assert "не сохранено" in event.message.answer.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [state for state in DialogState if state != DialogState.AWAITING_SUMMARY])
async def test_recovery_does_not_finalize_a_different_live_step(state):
    from contextlib import asynccontextmanager

    from aemr_bot.handlers import appeal_runtime

    user = SimpleNamespace(dialog_state=state.value)

    @asynccontextmanager
    async def current_user(_uid):
        yield object(), user

    create = AsyncMock()
    reset = AsyncMock()
    with patch.object(appeal_runtime, "current_user", current_user), \
         patch.object(appeal_runtime.appeals_service, "create_appeal", create), \
         patch.object(appeal_runtime.users_service, "reset_state", reset):
        assert await appeal_runtime.persist_and_dispatch_appeal(object(), 42, reset_empty=True) is None
    create.assert_not_awaited()
    reset.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("reset_empty", [True, False])
async def test_empty_recovery_reset_is_inside_finalization_transaction(reset_empty):
    from contextlib import asynccontextmanager

    from aemr_bot.handlers import appeal_runtime

    user = SimpleNamespace(id=1, first_name="Иван", is_blocked=False, consent_pdn_at=datetime.now(UTC),
                           dialog_state=DialogState.AWAITING_SUMMARY.value, dialog_data={})
    in_transaction = False

    @asynccontextmanager
    async def current_user(_uid):
        nonlocal in_transaction
        in_transaction = True
        yield object(), user
        in_transaction = False

    async def reset(_session, uid):
        assert in_transaction
        assert appeal_runtime.get_user_lock(uid).locked()

    reset_mock = AsyncMock(side_effect=reset)
    with patch.object(appeal_runtime, "current_user", current_user), \
         patch.object(appeal_runtime.appeals_service, "count_recent_for_user", AsyncMock(return_value=0)), \
         patch.object(appeal_runtime.users_service, "reset_state", reset_mock):
        assert await appeal_runtime.persist_and_dispatch_appeal(object(), 42, reset_empty=reset_empty) is False
    assert reset_mock.await_count == int(reset_empty)
    assert not in_transaction


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot", [None, {"consent_prompt_hash": "a" * 64},
                                      {"consent_prompt_hash": "a" * 64, "consent_prompt_mid": "old-prompt"}])
async def test_consent_evidence_is_bound_to_displayed_prompt(snapshot):
    from aemr_bot.handlers import appeal

    user = SimpleNamespace(is_blocked=False, dialog_data=snapshot)
    event = make_event(chat_id=100, user_id=42)
    accept = AsyncMock(return_value=True)
    prompt = AsyncMock()
    current_hash = AsyncMock(return_value="b" * 64)
    with patch.object(appeal, "session_scope", fake_session_scope), \
         patch.object(appeal.users_service, "get_or_create", AsyncMock(return_value=user)), \
         patch.object(appeal.users_service, "set_consent", accept), \
         patch.object(appeal.settings_store, "get_consent_text_hash", current_hash), \
         patch.object(appeal, "ack_callback", AsyncMock()), \
         patch.object(appeal, "get_callback_message_id", return_value="current-prompt"), \
         patch.object(appeal.admin_events, "notify_consent_given", AsyncMock()), \
         patch.object(appeal.appeal_funnel, "ask_contact_or_skip", AsyncMock()), \
         patch.object(appeal.appeal_funnel, "start_appeal_flow", prompt):
        await appeal._cb_consent_yes(event, 42, "consent:yes")
    current_hash.assert_not_awaited()
    if snapshot and "consent_prompt_mid" not in snapshot:
        assert accept.await_args.kwargs["text_sha256"] == "a" * 64
        prompt.assert_not_awaited()
    else:
        accept.assert_not_awaited()
        prompt.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("from_contact", [True, False])
async def test_oversized_phone_is_rejected_before_database_write(from_contact):
    event = make_event(chat_id=100, user_id=42)
    phone = "7" * 33
    save = AsyncMock()
    with patch.object(appeal_funnel, "extract_resident_contact", return_value=(phone if from_contact else None, None, False)), \
         patch.object(appeal_funnel.users_service, "set_phone", save):
        await appeal_funnel.on_awaiting_contact(event, None, "" if from_contact else phone, 42)
    save.assert_not_awaited()
    assert event.message.answer.await_count == 1
