from __future__ import annotations

import logging

from aemr_bot import keyboards, texts
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import DialogState
from aemr_bot.db.session import session_scope
from aemr_bot.handlers.appeal_runtime import _HAS_ALNUM
from aemr_bot.services import users as users_service

log = logging.getLogger(__name__)


async def ask_address(event, max_user_id: int) -> None:
    from aemr_bot.handlers import appeal_funnel

    await appeal_funnel._show_progress_step(
        event,
        max_user_id,
        stage="address",
        next_state=DialogState.AWAITING_ADDRESS,
        keyboard=keyboards.cancel_keyboard(),
        force_new_message=True,
    )


async def ask_summary(event, max_user_id: int) -> None:
    from aemr_bot.handlers import appeal_funnel

    await appeal_funnel._show_progress_step(
        event,
        max_user_id,
        stage="summary",
        next_state=DialogState.AWAITING_SUMMARY,
        keyboard=keyboards.cancel_keyboard(),
        force_new_message=True,
    )


async def on_awaiting_address(event, body, text_body, max_user_id: int) -> None:
    from aemr_bot.handlers import appeal_funnel

    address = text_body.strip()[: cfg.address_max_chars]
    if not address or not _HAS_ALNUM.search(address):
        await event.message.answer(texts.ADDRESS_EMPTY)
        return
    async with session_scope() as session:
        await users_service.update_dialog_data(session, max_user_id, {"address": address})
    await appeal_funnel.ask_topic(event, max_user_id, force_new_message=True)


def install() -> None:
    from aemr_bot.handlers import appeal, appeal_funnel

    appeal_funnel.ask_address = ask_address
    appeal_funnel.ask_summary = ask_summary
    appeal_funnel.on_awaiting_address = on_awaiting_address
    appeal._STATE_HANDLERS[DialogState.AWAITING_ADDRESS] = on_awaiting_address
    log.info("flow prompt policy installed")
