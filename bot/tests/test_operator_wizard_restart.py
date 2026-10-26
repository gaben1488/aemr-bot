"""New registrations must not reuse a previous target's approved details."""
from unittest.mock import AsyncMock, patch

import pytest

from aemr_bot.handlers import admin_operators_wizard as wizard
from tests._helpers import make_event


@pytest.fixture(autouse=True)
def clean_wizard():
    wizard._op_wizards.clear()
    yield
    wizard._op_wizards.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["manual", "group"])
async def test_restart_discards_previous_registration(source):
    event = make_event(chat_id=123, user_id=7)
    event.bot.get_chat_member = AsyncMock(return_value=None)
    with patch.object(wizard, "op_screen", AsyncMock()):
        wizard._op_wizard_set(
            7, step="ready_to_confirm", target_id=42, role="it",
            full_name="Previous Operator", suggested_name="Previous Name",
        )
        if source == "manual":
            await wizard._start_manual_add(event, 7)
        else:
            await wizard._start_add_with_picked(event, 7, 43)
    state = wizard._op_wizard_get(7)
    assert "role" not in state
    assert "full_name" not in state
    assert state.get("suggested_name") is None
    assert state.get("target_id") == (43 if source == "group" else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["awaiting_id", "awaiting_role", "picked_role", "awaiting_name"])
async def test_stale_confirmation_does_not_save_unconfirmed_step(step):
    event = make_event(chat_id=123, user_id=7)
    wizard._op_wizard_set(
        7, step=step, target_id=42, role="it", full_name="Old Name",
    )
    with (
        patch.object(wizard, "op_screen", AsyncMock()),
        patch.object(wizard, "session_scope") as scope,
    ):
        await wizard._confirm_save(event, 7)
    scope.assert_not_called()


@pytest.mark.asyncio
async def test_group_registration_cancels_competing_reply_and_broadcast():
    from aemr_bot.handlers import broadcast, operator_reply
    from aemr_bot.services import wizard_registry

    event = make_event(chat_id=123, user_id=7)
    event.bot.get_chat_member = AsyncMock(return_value=None)
    operator_reply.remember_reply_intent(7, 42)
    with (
        patch.object(broadcast, "_wizards", {7: object()}) as broadcasts,
        patch.object(wizard, "op_screen", AsyncMock()),
    ):
        try:
            await wizard._start_add_with_picked(event, 7, 43)
            assert 7 not in broadcasts
            assert wizard_registry.get_reply_intent(7) is None
        finally:
            operator_reply.drop_reply_intent(7)
