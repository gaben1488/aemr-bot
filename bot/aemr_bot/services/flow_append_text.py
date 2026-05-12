from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aemr_bot import keyboards
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import DialogState
from aemr_bot.db.session import session_scope
from aemr_bot.handlers.appeal_runtime import send_to_admin_card
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import card_format
from aemr_bot.services import users as users_service
from aemr_bot.utils.attachments import collect_attachments

log = logging.getLogger(__name__)

OPEN_STATUSES = {"new", "in_progress"}


async def append_text(event, body, text_body, max_user_id: int) -> None:
    from aemr_bot.handlers.menu import open_main_menu
    from aemr_bot.services.admin_relay import relay_attachments_to_admin

    text = (text_body or "").strip()
    attachments = collect_attachments(body)
    if not text and not attachments:
        await event.message.answer("Send one message with additional details or attach media/file.", attachments=[keyboards.cancel_keyboard()])
        return

    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        raw_id = (user.dialog_data or {}).get("appeal_id")
        appeal = await appeals_service.get_by_id(session, int(raw_id)) if raw_id else None
        if appeal is None or appeal.user_id != user.id or appeal.status not in OPEN_STATUSES:
            await users_service.reset_state(session, max_user_id)
            await event.message.answer("Appeal is no longer open. Submit a similar appeal instead.", attachments=[keyboards.back_to_menu_keyboard()])
            return

        now_local = datetime.now(ZoneInfo(cfg.timezone)).strftime("%d.%m.%Y %H:%M")
        block = f"Append {now_local}:\n{text or '(no text)'}"
        if attachments:
            block += f"\nAttachments: {len(attachments)}"
        old_summary = (appeal.summary or "").rstrip()
        appeal.summary = f"{old_summary}\n\n----------------\n{block}" if old_summary else block
        if attachments:
            appeal.attachments = list(appeal.attachments or []) + attachments
        await appeals_service.add_user_message(session, appeal=appeal, text=text or None, attachments=attachments)
        await users_service.reset_state(session, max_user_id)
        await session.flush()
        card_text = card_format.admin_card(appeal, user)
        card_keyboard = keyboards.appeal_admin_actions(appeal.id, appeal.status, is_it=True, user_blocked=user.is_blocked, closed_due_to_revoke=appeal.closed_due_to_revoke)
        admin_mid = appeal.admin_message_id
        appeal_id = appeal.id

    if cfg.admin_group_id and admin_mid:
        try:
            await event.bot.edit_message(message_id=admin_mid, text=card_text, attachments=[card_keyboard])
        except Exception:
            log.exception("failed to edit admin card after append")
            await send_to_admin_card(event.bot, card_text, appeal_id=appeal_id, status="in_progress")
    elif cfg.admin_group_id:
        await send_to_admin_card(event.bot, card_text, appeal_id=appeal_id, status="in_progress")

    if cfg.admin_group_id and attachments:
        try:
            await relay_attachments_to_admin(event.bot, appeal_id=appeal_id, admin_mid=admin_mid, stored_attachments=attachments)
        except Exception:
            log.exception("failed to relay append attachments")

    await event.message.answer(f"Added to appeal #{appeal_id}.")
    await open_main_menu(event)


def install() -> None:
    from aemr_bot.handlers import appeal

    appeal._STATE_HANDLERS[DialogState.AWAITING_FOLLOWUP_TEXT] = append_text
    log.info("append text policy installed")
