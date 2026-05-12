from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_deliver_operator_reply_blocks_closed_appeal() -> None:
    """Операторский ответ по CLOSED обращению не должен уходить жителю.

    Раньше service-layer не менял статус CLOSED обратно на ANSWERED, но
    сама доставка жителю всё равно происходила. Это опасно для старых
    карточек в админ-чате: оператор мог ответить свайпом или /reply по
    уже закрытому обращению без явного /reopen.
    """
    from aemr_bot.db.models import AppealStatus
    from aemr_bot.handlers import operator_reply

    event = SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock()),
    )
    appeal = SimpleNamespace(id=42)
    operator = SimpleNamespace(id=7, max_user_id=7001)
    fresh_appeal = SimpleNamespace(
        id=42,
        status=AppealStatus.CLOSED.value,
        user=SimpleNamespace(max_user_id=1001),
    )

    @asynccontextmanager
    async def fake_session_scope():
        yield SimpleNamespace()

    with patch("aemr_bot.handlers.operator_reply.session_scope", fake_session_scope), \
         patch(
             "aemr_bot.handlers.operator_reply.appeals_service.get_by_id",
             AsyncMock(return_value=fresh_appeal),
         ), \
         patch(
             "aemr_bot.handlers.operator_reply.get_chat_id",
             return_value=123,
         ), \
         patch(
             "aemr_bot.handlers.operator_reply._has_recent_successful_reply",
             return_value=False,
         ), \
         patch(
             "aemr_bot.handlers.operator_reply._reply_success_key",
             return_value=None,
         ), \
         patch(
             "aemr_bot.handlers.operator_reply._is_reply_success_recorded",
             AsyncMock(return_value=False),
         ), \
         patch(
             "aemr_bot.handlers.operator_reply.card_format.citizen_reply",
         ) as citizen_reply, \
         patch(
             "aemr_bot.handlers.operator_reply.appeals_service.add_operator_message",
             AsyncMock(),
         ) as add_operator_message:
        handled = await operator_reply._deliver_operator_reply(
            event,
            appeal=appeal,
            operator=operator,
            text="Ответ по уже закрытому обращению",
            audit_action="reply_via_command",
        )

    assert handled is True
    citizen_reply.assert_not_called()
    add_operator_message.assert_not_awaited()
    event.bot.send_message.assert_awaited_once()
    kwargs = event.bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 123
    assert "обращение уже закрыто" in kwargs["text"]
    assert "user_id" not in kwargs
