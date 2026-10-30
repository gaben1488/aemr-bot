"""Durable admin notification for saved resident follow-ups.

Claim before the external effect. Only pending work is replayable; a crash
or timeout during delivery is surfaced through diagnostics and never retried
blindly. The appeal remains the source of truth for text and attachments.
"""
from __future__ import annotations

import logging

from sqlalchemy import select, update

from aemr_bot.config import settings
from aemr_bot.db.models import Message
from aemr_bot.db.session import session_scope
from aemr_bot.services import appeals, privacy_epoch

log = logging.getLogger(__name__)


async def _warn_unknown(bot, message_id: int) -> None:
    """Reserve a single critical warning before contacting MAX."""
    from aemr_bot.services import admin_bus

    async with session_scope() as session:
        result = await session.execute(
            update(Message).where(Message.id == message_id,
                                  Message.admin_notice_state == "unknown")
            .values(admin_notice_state="alerting").returning(Message.appeal_id)
        )
        appeal_id = result.scalar_one_or_none()
    if appeal_id is None:
        return
    try:
        await admin_bus.send(
            bot,
            text=(f"⚠️ Дополнение к обращению № {appeal_id} сохранено, но публикация "
                  "карточки не подтверждена. Проверьте чат и историю обращения; "
                  "не отправляйте сообщение повторно вслепую."),
            critical=True,
        )
    except Exception as exc:
        log.warning("followup delivery warning not confirmed (%s)", type(exc).__name__)
    finally:
        # Even an exception can mean MAX accepted the warning. Never repeat it
        # automatically; diagnostics keeps the underlying outcome visible.
        async with session_scope() as session:
            await session.execute(update(Message).where(
                Message.id == message_id, Message.admin_notice_state == "alerting"
            ).values(admin_notice_state="unknown_alerted"))


async def deliver(bot, message_id: int) -> bool:
    """Deliver one pending notification and return whether the card was confirmed."""
    if not settings.admin_group_id:
        return False
    # Erasure acquires the exclusive counterpart before modifying rows. The
    # shared lock therefore spans the claim, MAX requests and result write.
    async with session_scope() as barrier:
        await privacy_epoch.hold_delivery(barrier)
        async with session_scope() as session:
            result = await session.execute(
                update(Message).where(Message.id == message_id,
                                      Message.admin_notice_state == "pending")
                .values(admin_notice_state="sending").returning(Message.appeal_id)
            )
            appeal_id = result.scalar_one_or_none()
        if appeal_id is None:
            return False
        async with session_scope() as session:
            appeal = await appeals.get_by_id_with_messages(session, appeal_id)
            message = await session.get(Message, message_id)
            attachments = list(message.attachments or []) if message else []
        if appeal is None or message is None:
            return False
        confirmed = False
        state = "unknown"
        try:
            from aemr_bot.services import admin_card
            from aemr_bot.services.admin_relay import relay_attachments_to_admin

            mid = await admin_card.render(
                bot, appeal, force_new=True,
                event_header=f"📩 Новое дополнение по обращению #{appeal_id} от жителя — см. историю.",
            )
            confirmed = bool(mid)
            if confirmed:
                complete = await relay_attachments_to_admin(
                    bot, appeal_id=appeal_id, admin_mid=mid, stored_attachments=attachments,
                ) if attachments else True
                state = "sent" if complete else "partial"
        except Exception as exc:
            # Exceptions may include message contents or attachment tokens.
            log.warning("followup delivery outcome unknown for #%s (%s)", appeal_id, type(exc).__name__)
        async with session_scope() as session:
            await session.execute(
                update(Message).where(Message.id == message_id,
                                      Message.admin_notice_state == "sending")
                .values(admin_notice_state=state)
            )
        if state == "unknown":
            await _warn_unknown(bot, message_id)
        return confirmed


async def recover_pending(bot, *, limit: int = 100) -> int:
    """Resume only work for which no send was started."""
    if not settings.admin_group_id:
        return 0
    async with session_scope() as session:
        ids = list(await session.scalars(
            select(Message.id).where(Message.admin_notice_state == "pending")
            .order_by(Message.id).limit(limit)
        ))
    count = 0
    for message_id in ids:
        try:
            count += int(await deliver(bot, message_id))
        except Exception:
            log.exception("followup recovery failed for message id %s", message_id)
    # A process may have stopped after recording unknown but before the
    # warning was reserved. Only an unreserved warning may be attempted.
    async with session_scope() as session:
        unresolved = list(await session.scalars(
            select(Message.id).where(Message.admin_notice_state == "unknown")
            .order_by(Message.id).limit(limit)
        ))
    for message_id in unresolved:
        try:
            await _warn_unknown(bot, message_id)
        except Exception:
            log.exception("followup warning recovery failed for message id %s", message_id)
    return count
