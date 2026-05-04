"""Operator-reply and citizen-followup logic, called from the unified
message_created handler in handlers/appeal.py.
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
from aemr_bot.utils.event import (
    extract_message_id,
    get_chat_id,
    get_message_link,
    get_user_id,
)

log = logging.getLogger(__name__)


def _mid_from_link(link) -> str | None:
    """Pull message-id from either the pydantic LinkedMessage or its dict
    fallback. love-apples/maxapi keeps it at `link.message.mid`; older
    revisions had `link.mid`. We try both."""
    inner = getattr(link, "message", None)
    if inner is not None:
        mid = getattr(inner, "mid", None)
        if mid is not None:
            return str(mid)
    if isinstance(link, dict):
        inner_dict = link.get("message")
        if isinstance(inner_dict, dict) and inner_dict.get("mid"):
            return str(inner_dict["mid"])
        if link.get("mid"):
            return str(link["mid"])
    mid = getattr(link, "mid", None)
    return str(mid) if mid is not None else None


def _extract_reply_target_mid(event) -> str | None:
    """Pull `mid` of the message being replied to from `event.message.link`.

    Verified against love-apples/maxapi: `Message.link: LinkedMessage | None`
    holds the reply/forward backref. The original message id lives at
    `link.message.mid` (the nested MessageBody), not at `link.mid`.
    """
    link = get_message_link(event)
    if link is None:
        return None

    link_type = getattr(link, "type", None)
    if link_type is None and isinstance(link, dict):
        link_type = link.get("type")
    # MessageLinkType.REPLY can arrive as the StrEnum value, the enum
    # member, or a plain string — match by lowercased suffix.
    if link_type is None or not str(link_type).lower().endswith("reply"):
        return None

    return _mid_from_link(link)


async def _deliver_operator_reply(
    event,
    *,
    appeal,
    operator,
    text: str,
    audit_action: str,
) -> bool:
    """Common path for delivering an operator's reply to a citizen.

    Used both by handle_operator_reply (swipe-to-reply mechanism, which
    depends on Message.link being populated by the MAX client) and by
    cmd_reply (explicit /reply <appeal_id> <text> command, which works
    on every client regardless of swipe support).

    Returns True if a definitive answer was given to the operator (either
    delivered, or politely refused due to length / undeliverable). Returns
    False only on the dedupe path when target_mid was None and the operator
    didn't actually intend to reply.
    """
    if len(text) > cfg.answer_max_chars:
        await event.bot.send_message(
            chat_id=get_chat_id(event),
            text=texts.ADMIN_REPLY_TOO_LONG.format(
                limit=cfg.answer_max_chars, actual=len(text)
            ),
        )
        return True

    target_user_id = appeal.user.max_user_id
    formatted_text = card_format.citizen_reply(appeal, text)
    try:
        # IMPORTANT: deliver to the citizen by user_id (not chat_id) — we never
        # stored their personal-dialog chat_id, only their MAX user_id.
        sent = await event.bot.send_message(user_id=target_user_id, text=formatted_text)
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
    delivered_mid = extract_message_id(sent)

    async with session_scope() as session:
        appeal_full = await appeals_service.get_by_id(session, appeal.id)
        if appeal_full is None:
            log.warning(
                "appeal #%s vanished between lookup and reload", appeal.id
            )
            return True
        await appeals_service.add_operator_message(
            session,
            appeal=appeal_full,
            text=text,
            operator_id=operator.id,
            max_message_id=delivered_mid,
        )
        await operators_service.write_audit(
            session,
            operator_max_user_id=operator.max_user_id,
            action=audit_action,
            target=f"appeal #{appeal.id}",
            details={"chars": len(text)},
        )

    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=texts.ADMIN_REPLY_DELIVERED.format(number=appeal.id),
    )
    return True


async def handle_operator_reply(event: MessageCreated, body, text: str) -> bool:
    """Operator replied to the admin-group card via swipe/«Ответить».

    Returns True if handled, False if the message wasn't a reply at all
    (so the dispatcher can route it elsewhere — currently nowhere).
    """
    target_mid = _extract_reply_target_mid(event)
    if target_mid is None:
        log.info(
            "operator_reply: no reply-link in event.message — message ignored "
            "(operator wrote in admin group without using reply/swipe)"
        )
        return False

    author_id = get_user_id(event)
    if author_id is None:
        log.warning("operator_reply: no user_id in event")
        return False

    async with session_scope() as session:
        operator = await operators_service.get(session, author_id)
        if operator is None:
            log.info(
                "operator_reply: user_id=%s replied but is not in operators table",
                author_id,
            )
            return False
        log.info(
            "operator_reply: detected — operator_id=%s reply_to_mid=%s text_len=%d",
            operator.id, target_mid, len(text),
        )

        appeal = await appeals_service.get_by_admin_message_id(session, target_mid)
        if appeal is None:
            await event.bot.send_message(
                chat_id=get_chat_id(event), text=texts.ADMIN_REPLY_NO_APPEAL
            )
            return True

    return await _deliver_operator_reply(
        event,
        appeal=appeal,
        operator=operator,
        text=text,
        audit_action="reply",
    )


async def handle_command_reply(event, appeal_id: int, text: str) -> None:
    """`/reply N <text>` from the admin group — alternative to swipe-reply.

    Useful when the MAX client doesn't put a reply-link on the swiped
    message (varies by client/version), or when the operator prefers
    explicit commands. Same delivery path, same audit, same answer-cap.
    """
    if not cfg.admin_group_id or get_chat_id(event) != cfg.admin_group_id:
        return

    author_id = get_user_id(event)
    if author_id is None:
        return

    async with session_scope() as session:
        operator = await operators_service.get(session, author_id)
        if operator is None:
            await event.bot.send_message(
                chat_id=cfg.admin_group_id, text=texts.OP_NOT_AUTHORIZED
            )
            return
        appeal = await appeals_service.get_by_id(session, appeal_id)
        if appeal is None:
            await event.bot.send_message(
                chat_id=cfg.admin_group_id,
                text=texts.OP_APPEAL_NOT_FOUND.format(number=appeal_id),
            )
            return

    log.info(
        "command_reply: operator_id=%s appeal=%s text_len=%d",
        operator.id, appeal_id, len(text),
    )
    await _deliver_operator_reply(
        event,
        appeal=appeal,
        operator=operator,
        text=text,
        audit_action="reply_via_command",
    )


async def handle_user_followup(event: MessageCreated, text: str) -> bool:
    """Citizen wrote in private dialog while idle — reopen answered appeal if any."""
    from aemr_bot.db.models import AppealStatus, DialogState

    max_user_id = get_user_id(event)
    if max_user_id is None:
        return False

    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        if user.dialog_state != DialogState.IDLE.value:
            return False
        active = await appeals_service.find_active_for_user(session, user.id)

    if active is None or active.status != AppealStatus.ANSWERED.value:
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
