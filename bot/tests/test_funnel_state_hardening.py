"""Regression-тесты hardening-слоя пользовательской FSM-воронки."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import update

from aemr_bot.db.models import DialogState, User
from aemr_bot.handlers import appeal
from aemr_bot.services import users as users_service

pytest.importorskip("maxapi", reason="dispatcher guard tests require maxapi")


def test_expected_funnel_callback_states() -> None:
    assert appeal._expected_funnel_callback_states("consent:yes") == (
        DialogState.AWAITING_CONSENT,
    )
    assert appeal._expected_funnel_callback_states("addr:reuse") == (
        DialogState.AWAITING_NAME,
        DialogState.AWAITING_LOCALITY,
    )
    assert appeal._expected_funnel_callback_states("locality:0") == (
        DialogState.AWAITING_LOCALITY,
    )
    assert appeal._expected_funnel_callback_states("geo:confirm") == (
        DialogState.AWAITING_GEO_CONFIRM,
    )
    assert appeal._expected_funnel_callback_states("topic:1") == (
        DialogState.AWAITING_TOPIC,
    )
    assert appeal._expected_funnel_callback_states("appeal:submit") == (
        DialogState.AWAITING_SUMMARY,
    )
    assert appeal._expected_funnel_callback_states("menu:main") == ()


def test_clear_geo_detected_keeps_confirmed_funnel_data() -> None:
    data = {
        "locality": "Елизовское ГП",
        "address": "old",
        "topic": "Дороги",
        "detected_locality": "Елизовское ГП",
        "detected_street": "Ленина",
        "detected_house_number": "5",
        "detected_lat": 53.1,
        "detected_lon": 158.3,
        "detected_confidence": "exact",
    }

    cleaned = appeal._clear_geo_detected(data)

    assert cleaned == {
        "locality": "Елизовское ГП",
        "address": "old",
        "topic": "Дороги",
    }
    assert data["detected_locality"] == "Елизовское ГП"


def test_clear_geo_detected_can_drop_locality() -> None:
    cleaned = appeal._clear_geo_detected(
        {
            "locality": "ошибка",
            "detected_locality": "ошибка",
            "detected_street": "Ленина",
            "summary_chunks": ["текст"],
        },
        drop_locality=True,
    )

    assert cleaned == {"summary_chunks": ["текст"]}


@asynccontextmanager
async def _fake_session_scope_for_state(dialog_state: DialogState):
    yield SimpleNamespace(dialog_state=dialog_state.value)


@pytest.mark.asyncio
async def test_stale_topic_callback_is_acked_and_ignored() -> None:
    event = SimpleNamespace(bot=MagicMock())
    get_or_create = AsyncMock(
        return_value=SimpleNamespace(dialog_state=DialogState.AWAITING_ADDRESS.value)
    )
    ack = AsyncMock()

    @asynccontextmanager
    async def fake_scope():
        yield MagicMock()

    with patch("aemr_bot.handlers.appeal.session_scope", fake_scope), patch(
        "aemr_bot.handlers.appeal.users_service.get_or_create", get_or_create
    ), patch("aemr_bot.handlers.appeal.ack_callback", ack):
        allowed = await appeal._ensure_funnel_callback_state(event, 7, "topic:0")

    assert allowed is False
    ack.assert_called_once_with(event)


@pytest.mark.asyncio
async def test_current_topic_callback_is_allowed() -> None:
    event = SimpleNamespace(bot=MagicMock())
    get_or_create = AsyncMock(
        return_value=SimpleNamespace(dialog_state=DialogState.AWAITING_TOPIC.value)
    )
    ack = AsyncMock()

    @asynccontextmanager
    async def fake_scope():
        yield MagicMock()

    with patch("aemr_bot.handlers.appeal.session_scope", fake_scope), patch(
        "aemr_bot.handlers.appeal.users_service.get_or_create", get_or_create
    ), patch("aemr_bot.handlers.appeal.ack_callback", ack):
        allowed = await appeal._ensure_funnel_callback_state(event, 7, "topic:0")

    assert allowed is True
    ack.assert_not_called()


@pytest.mark.asyncio
async def test_find_stuck_in_funnel_includes_geo_confirm(session) -> None:
    user = await users_service.get_or_create(session, max_user_id=404, first_name="Geo")
    await users_service.set_state(
        session,
        404,
        DialogState.AWAITING_GEO_CONFIRM,
        data={"detected_locality": "Елизовское ГП"},
    )
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    await session.execute(update(User).where(User.id == user.id).values(updated_at=old))
    await session.flush()

    rows = await users_service.find_stuck_in_funnel(session, idle_seconds=3600)
    assert (404, DialogState.AWAITING_GEO_CONFIRM.value) in rows
