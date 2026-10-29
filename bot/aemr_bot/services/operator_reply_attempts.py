"""Durable reply attempt states; never replay an uncertain MAX request."""
from __future__ import annotations

import logging

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from aemr_bot.db.models import AppealStatus, Message, Operator, OperatorReplyAttempt
from aemr_bot.db.session import session_scope
from aemr_bot.services import appeal_locks, appeals, operators, privacy_epoch

log = logging.getLogger(__name__)


async def reserve(*, appeal_id: int, operator_id: int, source_key: str | None,
                  text: str, attachments: list, is_final: bool) -> int | None:
    """Persist content and claim its input event before starting the send."""
    async with session_scope() as session:
        statement = insert(OperatorReplyAttempt).values(
            appeal_id=appeal_id, operator_id=operator_id, source_key=source_key,
            text=text, attachments=attachments, is_final=is_final, state="sending",
        ).on_conflict_do_nothing(index_elements=[OperatorReplyAttempt.source_key])
        return (await session.execute(statement.returning(OperatorReplyAttempt.id))).scalar_one_or_none()


async def mark_unknown(attempt_id: int) -> None:
    async with session_scope() as session:
        await session.execute(update(OperatorReplyAttempt).where(
            OperatorReplyAttempt.id == attempt_id, OperatorReplyAttempt.state == "sending",
        ).values(state="unknown"))


async def mark_confirmed(attempt_id: int, mid: str) -> None:
    async with session_scope() as session:
        await session.execute(update(OperatorReplyAttempt).where(
            OperatorReplyAttempt.id == attempt_id, OperatorReplyAttempt.state == "sending",
        ).values(state="confirmed", max_message_id=mid, confirmed_at=func.now()))


async def _can_apply_status(session, appeal, attempt) -> bool:
    """Do not let a late recovery overwrite a newer message or reopened case."""
    if appeal.status not in {AppealStatus.NEW.value, AppealStatus.IN_PROGRESS.value}:
        return False
    if appeal.sla_anchor_at and appeal.sla_anchor_at > attempt.created_at:
        return False
    newer = await session.scalar(select(Message.id).where(
        Message.appeal_id == attempt.appeal_id, Message.created_at > attempt.created_at,
    ).limit(1))
    return newer is None


async def record_confirmed(session, attempt_id: int, *, mid: str | None = None,
                           audit_action: str = "reply_recovered") -> bool:
    """Record confirmed text once, in the same transaction as the state change."""
    attempt = await session.scalar(select(OperatorReplyAttempt).where(
        OperatorReplyAttempt.id == attempt_id).with_for_update())
    if attempt is None:
        return False
    if attempt.state == "recorded":
        return True
    delivery_mid = mid or attempt.max_message_id
    if not delivery_mid or attempt.state not in {"sending", "confirmed"}:
        return False
    appeal = await appeals.get_by_id(session, attempt.appeal_id, for_update=True)
    if appeal is None:
        return False
    existing = await session.scalar(select(Message.id).where(
        Message.appeal_id == attempt.appeal_id, Message.max_message_id == delivery_mid,
        Message.direction == "from_operator").limit(1))
    if existing is None:
        await appeals.add_operator_message(
            session, appeal=appeal, text=attempt.text or "", operator_id=attempt.operator_id,
            max_message_id=delivery_mid, is_final=attempt.is_final, attachments=attempt.attachments,
            apply_status=await _can_apply_status(session, appeal, attempt),
            created_at=attempt.confirmed_at, delivery_source="max",
        )
        operator = await session.get(Operator, attempt.operator_id) if attempt.operator_id else None
        await operators.write_audit(
            session, operator_max_user_id=operator.max_user_id if operator else None,
            action=audit_action, target=f"appeal #{attempt.appeal_id}",
            details={"chars": len(attempt.text or "")},
        )
    attempt.state = "recorded"
    attempt.max_message_id = delivery_mid
    # The canonical message now owns the content; keep only dedupe metadata.
    attempt.text = None
    attempt.attachments = []
    return True


async def recover_confirmed(*, limit: int = 100) -> int:
    """Repair local history only; no request to MAX is made here."""
    async with session_scope() as session:
        rows = list((await session.execute(select(OperatorReplyAttempt.id, OperatorReplyAttempt.appeal_id).where(
            OperatorReplyAttempt.state == "confirmed").order_by(OperatorReplyAttempt.id).limit(limit))).all())
    count = 0
    for attempt_id, appeal_id in rows:
        try:
            async with session_scope() as barrier:
                await privacy_epoch.hold_delivery(barrier)
                await appeal_locks.hold(barrier, appeal_id)
                async with session_scope() as session:
                    count += int(await record_confirmed(session, attempt_id))
        except Exception:
            log.exception("operator reply recovery failed for attempt %s", attempt_id)
    return count


async def resolve_unknown(session, attempt_id: int, *, delivered: bool,
                          actor_max_id: int) -> bool:
    """Apply an operator's explicit reconciliation under the appeal lock.

    The caller holds the privacy and appeal advisory locks; this function
    never contacts MAX and never fabricates a MAX message identifier.
    """
    attempt = await session.scalar(select(OperatorReplyAttempt).where(
        OperatorReplyAttempt.id == attempt_id).with_for_update())
    if not attempt or attempt.state not in {"sending", "unknown"}:
        return False
    appeal = await appeals.get_by_id(session, attempt.appeal_id, for_update=True)
    if not appeal:
        return False
    if delivered:
        await appeals.add_operator_message(
            session, appeal=appeal, text=attempt.text or "", operator_id=attempt.operator_id,
            max_message_id=None, is_final=attempt.is_final, attachments=attempt.attachments,
            apply_status=await _can_apply_status(session, appeal, attempt),
            delivery_source="manual",
        )
        attempt.state = "recorded"
        attempt.text = None
        attempt.attachments = []
    else:
        # Preserve the draft so the operator can copy it into a new reply.
        # No resend is initiated by this action.
        attempt.state = "not_delivered"
    await operators.write_audit(
        session, operator_max_user_id=actor_max_id, action="reply_reconciled",
        target=f"appeal #{attempt.appeal_id}",
        details={"result": "confirmed_delivered" if delivered else "confirmed_not_delivered"},
    )
    return True


async def discard_not_delivered(session, attempt_id: int, *, actor_max_id: int) -> bool:
    attempt = await session.scalar(select(OperatorReplyAttempt).where(
        OperatorReplyAttempt.id == attempt_id).with_for_update())
    if not attempt or attempt.state != "not_delivered":
        return False
    attempt.state = "discarded"
    attempt.text = None
    attempt.attachments = []
    await operators.write_audit(session, operator_max_user_id=actor_max_id,
                                action="reply_draft_discarded", target=f"appeal #{attempt.appeal_id}")
    return True
