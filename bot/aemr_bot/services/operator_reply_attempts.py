"""Durable reply attempt states; never replay an uncertain MAX request."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from aemr_bot.db.models import (
    AppealStatus,
    Message,
    Operator,
    OperatorReplyAttempt,
    OperatorReplyDraft,
)
from aemr_bot.db.session import session_scope
from aemr_bot.services import appeal_locks, appeals, operators, privacy_epoch

log = logging.getLogger(__name__)
# A permanently unrepairable early record must not starve later records.
# This cursor affects only local history recovery, never external delivery.
_recovery_cursor = 0


def reconciliation_revision(attempt) -> str:
    """Bind a confirmation to the exact attempt shown to the operator."""
    payload = [attempt.state, attempt.text, attempt.attachments, attempt.is_final, attempt.delivery_parts]
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def reserve(*, appeal_id: int, operator_id: int, source_key: str | None,
                  text: str, attachments: list, is_final: bool,
                  part_plan: list[list[int]] | None = None) -> int | None:
    """Persist content and claim its input event before starting the send."""
    async with session_scope() as session:
        statement = insert(OperatorReplyAttempt).values(
            appeal_id=appeal_id, operator_id=operator_id, source_key=source_key,
            text=text, attachments=attachments, is_final=is_final, state="sending",
            last_activity_at=func.now(),
            delivery_parts=[{"index": i, "state": "planned", "mid": None, "attachments": indices}
                            for i, indices in enumerate(part_plan)] if part_plan is not None else [],
        ).on_conflict_do_nothing(index_elements=[OperatorReplyAttempt.source_key])
        return (await session.execute(statement.returning(OperatorReplyAttempt.id))).scalar_one_or_none()


async def note_part(attempt_id: int, index: int, state: str, mid: str | None) -> None:
    """Commit the per-part transport journal without retrying MAX."""
    async with session_scope() as session:
        attempt = await session.scalar(select(OperatorReplyAttempt).where(
            OperatorReplyAttempt.id == attempt_id).with_for_update())
        if attempt is None or attempt.state != "sending":
            raise ValueError("Reply attempt is no longer sending")
        parts = list(attempt.delivery_parts or [])
        if index > len(parts):
            raise ValueError("Reply parts out of order")
        record = {"index": index, "state": state, "mid": mid}
        if index < len(parts) and "attachments" in parts[index]:
            if (state, parts[index]["state"]) not in {("sending", "planned"), ("confirmed", "sending")}:
                raise ValueError("Invalid reply part transition")
            record["attachments"] = parts[index]["attachments"]
            if state == "confirmed":
                record["confirmed_at"] = datetime.now(UTC).isoformat()
        if index == len(parts):
            parts.append(record)
        else:
            parts[index] = record
        attempt.delivery_parts = parts
        attempt.last_activity_at = func.now()
        if parts and all(p.get("state") == "confirmed" and "attachments" in p for p in parts):
            attempt.state = "confirmed"
            attempt.max_message_id = parts[0].get("mid")
            attempt.confirmed_at = datetime.fromisoformat(parts[0]["confirmed_at"])


async def mark_unknown(attempt_id: int) -> None:
    async with session_scope() as session:
        await session.execute(update(OperatorReplyAttempt).where(
            OperatorReplyAttempt.id == attempt_id, OperatorReplyAttempt.state == "sending",
        ).values(state="unknown", last_activity_at=func.now()))


async def mark_confirmed(attempt_id: int, mid: str) -> None:
    async with session_scope() as session:
        await session.execute(update(OperatorReplyAttempt).where(
            OperatorReplyAttempt.id == attempt_id, OperatorReplyAttempt.state == "sending",
        ).values(state="confirmed", max_message_id=mid, confirmed_at=func.now(), last_activity_at=func.now()))


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


async def _release_composition(session, attempt) -> None:
    source = attempt.source_key or ""
    if source.startswith("reply_draft:"):
        await session.execute(delete(OperatorReplyDraft).where(
            OperatorReplyDraft.token == source.removeprefix("reply_draft:"),
            OperatorReplyDraft.state == "sending",
        ))


async def record_confirmed(session, attempt_id: int, *, mid: str | None = None,
                           audit_action: str = "reply_recovered") -> bool:
    """Record confirmed text once, in the same transaction as the state change."""
    attempt = await session.scalar(select(OperatorReplyAttempt).where(
        OperatorReplyAttempt.id == attempt_id).with_for_update())
    if attempt is None:
        return False
    if attempt.state == "recorded":
        await _release_composition(session, attempt)
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
    attempt.last_activity_at = func.now()
    attempt.max_message_id = delivery_mid
    # The canonical message now owns the content; keep only dedupe metadata.
    attempt.text = None
    attempt.attachments = []
    await _release_composition(session, attempt)
    return True


async def recover_confirmed(*, limit: int = 100) -> int:
    """Repair local history only; no request to MAX is made here."""
    global _recovery_cursor
    if limit <= 0:
        return 0
    async with session_scope() as session:
        rows = list((await session.execute(select(OperatorReplyAttempt.id, OperatorReplyAttempt.appeal_id).where(
            OperatorReplyAttempt.state == "confirmed", OperatorReplyAttempt.id > _recovery_cursor
        ).order_by(OperatorReplyAttempt.id).limit(limit))).all())
        if not rows and _recovery_cursor:
            _recovery_cursor = 0
            rows = list((await session.execute(select(OperatorReplyAttempt.id, OperatorReplyAttempt.appeal_id).where(
                OperatorReplyAttempt.state == "confirmed"
            ).order_by(OperatorReplyAttempt.id).limit(limit))).all())
    if rows:
        _recovery_cursor = rows[-1][0]
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
                          actor_max_id: int, expected_revision: str | None = None) -> bool:
    """Apply an operator's explicit reconciliation under the appeal lock.

    The caller holds the privacy and appeal advisory locks; this function
    never contacts MAX and never fabricates a MAX message identifier.
    """
    attempt = await session.scalar(select(OperatorReplyAttempt).where(
        OperatorReplyAttempt.id == attempt_id).with_for_update())
    if not attempt or attempt.state not in {"sending", "unknown"}:
        return False
    if expected_revision is not None and reconciliation_revision(attempt) != expected_revision:
        return False
    appeal = await appeals.get_by_id(session, attempt.appeal_id, for_update=True)
    if not appeal:
        return False
    parts = list(attempt.delivery_parts or [])
    if parts and all("attachments" in part for part in parts):
        return await _resolve_planned(session, attempt, appeal, parts,
                                      delivered=delivered, actor_max_id=actor_max_id)
    if not delivered and (len(parts) > 1 or any(part.get("state") == "confirmed" for part in parts)):
        raise ValueError("Старый ответ без плана частей: требуется ручная сверка истории и вложений.")
    attempt.last_activity_at = func.now()
    if delivered:
        await appeals.add_operator_message(
            session, appeal=appeal, text=attempt.text or "", operator_id=attempt.operator_id,
            max_message_id=None, is_final=attempt.is_final, attachments=attempt.attachments,
            apply_status=await _can_apply_status(session, appeal, attempt),
            created_at=attempt.created_at, delivery_source="manual_partial",
            delivery_uncertain_until=datetime.now(UTC),
        )
        attempt.state = "recorded"
        attempt.text = None
        attempt.attachments = []
    else:
        # Preserve the draft so the operator can copy it into a new reply.
        # No resend is initiated by this action.
        attempt.state = "not_delivered"
    await _release_composition(session, attempt)
    await operators.write_audit(
        session, operator_max_user_id=actor_max_id, action="reply_reconciled",
        target=f"appeal #{attempt.appeal_id}",
        details={"result": "confirmed_delivered" if delivered else "confirmed_not_delivered"},
    )
    return True


async def _resolve_planned(session, attempt, appeal, parts, *, delivered, actor_max_id):
    """Record only verified batches and retain unsent content for a new reply."""
    indices = [index for part in parts for index in part.get("attachments", [])]
    states = [part.get("state") for part in parts]
    if (indices != list(range(len(attempt.attachments)))
            or [part.get("index") for part in parts] != list(range(len(parts)))
            or any(state not in {"confirmed", "sending", "planned"} for state in states)
            or states.count("sending") > 1):
        raise ValueError("План частей повреждён: требуется ручная сверка.")
    uncertain = states.index("sending") if "sending" in states else None
    boundary = uncertain if uncertain is not None else states.count("confirmed")
    if states[:boundary] != ["confirmed"] * boundary or states[boundary + (uncertain is not None):] != ["planned"] * (len(parts) - boundary - (uncertain is not None)):
        raise ValueError("Порядок частей нарушен: требуется ручная сверка.")
    if uncertain is None and delivered:
        raise ValueError("Нет неопределённой части для подтверждения доставки.")
    if uncertain is not None:
        parts[uncertain] = {**parts[uncertain], "state": "manual_confirmed" if delivered else "not_delivered"}
    attempt.last_activity_at = func.now()
    verified = [part for part in parts if part["state"] in {"confirmed", "manual_confirmed"}]
    verified_indices = {index for part in verified for index in part["attachments"]}
    first_delivered = bool(verified and verified[0]["index"] == 0)
    complete = len(verified) == len(parts)
    if first_delivered:
        first_time = parts[0].get("confirmed_at")
        await appeals.add_operator_message(
            session, appeal=appeal, text=attempt.text or "", operator_id=attempt.operator_id,
            max_message_id=parts[0].get("mid"), is_final=complete and attempt.is_final,
            attachments=[item for i, item in enumerate(attempt.attachments) if i in verified_indices],
            apply_status=await _can_apply_status(session, appeal, attempt),
            created_at=datetime.fromisoformat(first_time) if first_time else attempt.created_at,
            delivery_source=("manual_partial" if not first_time else
                             "manual_mixed" if delivered else "max"),
            delivery_uncertain_until=None if first_time else datetime.now(UTC),
        )
    attempt.delivery_parts = parts
    if complete:
        attempt.state = "recorded"
        attempt.text = None
        attempt.attachments = []
    else:
        attempt.state = "not_delivered"
        if first_delivered:
            attempt.text = None
        attempt.attachments = [item for i, item in enumerate(attempt.attachments) if i not in verified_indices]
    await _release_composition(session, attempt)
    await operators.write_audit(session, operator_max_user_id=actor_max_id,
                                action="reply_reconciled", target=f"appeal #{attempt.appeal_id}",
                                details={"result": "complete" if complete else "partial"})
    return True


async def discard_not_delivered(session, attempt_id: int, *, actor_max_id: int,
                                expected_revision: str | None = None) -> bool:
    attempt = await session.scalar(select(OperatorReplyAttempt).where(
        OperatorReplyAttempt.id == attempt_id).with_for_update())
    if not attempt or attempt.state != "not_delivered":
        return False
    if expected_revision is not None and reconciliation_revision(attempt) != expected_revision:
        return False
    attempt.last_activity_at = func.now()
    attempt.state = "discarded"
    attempt.text = None
    attempt.attachments = []
    await operators.write_audit(session, operator_max_user_id=actor_max_id,
                                action="reply_draft_discarded", target=f"appeal #{attempt.appeal_id}")
    return True
