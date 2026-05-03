"""Operator-reply and citizen-followup logic, called from the unified message_created
handler in handlers/appeal.py. No decorators here — registering two
@dp.message_created() handlers risks double-processing or shadowing.
"""

import logging

from maxapi import Dispatcher
from maxapi.types import MessageCreated

from aemr_bot import texts
from aemr_bot.config import settings as cfg
from aemr_bot.db.session import session_scope
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import card_format
from aemr_bot.services import operators as operators_service
from aemr_bot.services import users as users_service
from aemr_bot.utils.event import get_chat_id, get_message_link, get_user_id

log = logging.getLogger(__name__)


def _extract_reply_target_mid(event) -> str | None:
    """Pull `mid` of the message being replied to.

    Verified against love-apples/maxapi `Message.link: LinkedMessage | None`
    (NOT `MessageBody.link` — body never had this field, that was a bug from
    the initial integration). Reply detection therefore must read from the
    Message object, which lives at `event.message.link`. We accept dict
    fallback in case of schema drift, and tolerate enum vs string for `type`.
    """
    link = get_message_link(event)
    if link is None:
        return None

    link_type = getattr(link, "type", None)
    if link_type is None and isinstance(link, dict):
        link_type = link.get("type")
    if link_type is None:
        return None
    # MessageLinkType.REPLY may arrive as the StrEnum ("reply"), as the enum
    # member, or as the bare string. Coerce both sides to lowercase strings.
    if str(link_type).lower().endswith("reply") is False:
        return None

    mid = getattr(link, "mid", None)
    if mid is None and isinstance(link, dict):
        mid = link.get("mid")
    return str(mid) if mid is not None else None


async def handle_operator_reply(event: MessageCreated, body, text: str) -> bool:
    """Operator replied to the admin-group card. Returns True if handled."""
    target_mid = _extract_reply_target_mid(event)
    if target_mid is None:
        return False

    author_id = get_user_id(event)
    if author_id is None:
        return False

    async with session_scope() as session:
        operator = await operators_service.get(session, author_id)
        if operator is None:
            return False

        appeal = await appeals_service.get_by_admin_message_id(session, target_mid)
        if appeal is None:
            await event.bot.send_message(chat_id=get_chat_id(event), text=texts.ADMIN_REPLY_NO_APPEAL)
            return True

    if len(text) > cfg.answer_max_chars:
        await event.bot.send_message(
            chat_id=get_chat_id(event),
            text=texts.ADMIN_REPLY_TOO_LONG.format(limit=cfg.answer_max_chars, actual=len(text)),
        )
        return True

    target_user_id = appeal.user.max_user_id
    try:
        # IMPORTANT: deliver to the citizen by user_id (not chat_id) — we never
        # stored their personal-dialog chat_id, only their MAX user_id.
        sent = await event.bot.send_message(user_id=target_user_id, text=text)
    except Exception as exc:  # noqa: BLE001
        await event.bot.send_message(
            chat_id=get_chat_id(event),
            text=(
                f"⚠️ Не удалось доставить ответ жителю по обращению #{appeal.id}: {exc}.\n"
                "Возможно, житель удалил диалог или заблокировал бота. "
                "Обращение остаётся в работе."
            ),
        )
        return True
    delivered_mid = getattr(sent, "message_id", None) or getattr(getattr(sent, "body", None), "mid", None)

    async with session_scope() as session:
        appeal_full = await appeals_service.get_by_id(session, appeal.id)
        if appeal_full is None:
            log.warning("appeal #%s vanished between get_by_admin_message_id and reload", appeal.id)
            return True
        await appeals_service.add_operator_message(
            session,
            appeal=appeal_full,
            text=text,
            operator_id=operator.id,
            max_message_id=str(delivered_mid) if delivered_mid is not None else None,
        )
        await operators_service.write_audit(
            session,
            operator_max_user_id=author_id,
            action="reply",
            target=f"appeal #{appeal.id}",
            details={"chars": len(text)},
        )

    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=texts.ADMIN_REPLY_DELIVERED.format(number=appeal.id),
    )
    return True


async def handle_user_followup(event: MessageCreated, text: str) -> bool:
    """Citizen wrote in private dialog while idle — reopen answered appeal if any."""
    max_user_id = get_user_id(event)
    if max_user_id is None:
        return False

    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        if user.dialog_state != "idle":
            return False
        active = await appeals_service.find_active_for_user(session, user.id)

    if active is None or active.status != "answered":
        return False

    async with session_scope() as session:
        await appeals_service.reopen(session, active.id)
        await appeals_service.add_user_message(
            session,
            appeal=active,
            text=text,
            attachments=[],
        )
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        followup = card_format.admin_followup(active, user, text)

    if cfg.admin_group_id:
        await event.bot.send_message(chat_id=cfg.admin_group_id, text=followup)
    return True


def register(dp: Dispatcher) -> None:
    """No-op: message_created routing is owned by handlers/appeal.py."""
    return None
