"""The MAX linked-message sender belongs to the link, not MessageBody."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from maxapi.types import LinkedMessage

from aemr_bot.handlers import operator_reply
from tests._helpers import fake_session_scope, make_event


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["sdk", "dict", "legacy"])
@pytest.mark.parametrize("link_type", ["reply", "forward"])
async def test_marker_fallback_requires_bot_authored_reply(shape, link_type):
    event = make_event(chat_id=123, user_id=7)
    operator_reply.drop_reply_intent(7)
    payload = {
        "type": link_type,
        "sender": {"is_bot": True, "user_id": 999, "first_name": "Bot"},
        "message": {"mid": "historical", "seq": 1, "text": "Card\n🆔 №42"},
    }
    if shape == "sdk":
        event.message.link = LinkedMessage.model_validate(payload)
    elif shape == "dict":
        event.message.link = payload
    else:
        event.message.link = SimpleNamespace(
            type=link_type,
            message=SimpleNamespace(
                mid="historical", text="Card\n🆔 №42",
                sender=SimpleNamespace(is_bot=True),
            ),
        )
    appeal = SimpleNamespace(id=42)
    with (
        patch.object(operator_reply, "session_scope", fake_session_scope),
        patch.object(operator_reply.operators_service, "get", AsyncMock(return_value=SimpleNamespace(id=1))),
        patch.object(operator_reply.appeals_service, "get_by_admin_message_id", AsyncMock(return_value=None)),
        patch.object(operator_reply.appeals_service, "get_by_last_admin_card_mid", AsyncMock(return_value=None)),
        patch.object(operator_reply.appeals_service, "get_by_id", AsyncMock(return_value=appeal)) as lookup,
        patch.object(operator_reply, "_deliver_operator_reply", AsyncMock(return_value=True)) as deliver,
    ):
        handled = await operator_reply.handle_operator_reply(event, event.message.body, "Reply")
    if link_type == "reply":
        assert handled
        lookup.assert_awaited_once()
        assert lookup.await_args.args[1] == 42
        deliver.assert_awaited_once()
    else:
        assert not handled
        lookup.assert_not_awaited()
        deliver.assert_not_awaited()
