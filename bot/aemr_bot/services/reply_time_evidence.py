"""First-reply evidence without treating manual reconciliation time as delivery time."""
from datetime import datetime

from sqlalchemy import and_, func, literal, or_, select, union_all

from aemr_bot.db.models import Appeal, Message, OperatorReplyAttempt

UNRECORDED_STATES = ("sending", "unknown", "confirmed")


def pending_in_period(anchor):
    """Fully confirmed old deliveries cannot affect a later reopened cycle."""
    return and_(
        OperatorReplyAttempt.state.in_(UNRECORDED_STATES),
        or_(OperatorReplyAttempt.state != "confirmed",
            OperatorReplyAttempt.last_activity_at.is_(None),
            OperatorReplyAttempt.last_activity_at >= anchor),
    )


async def first_reply_times(session, appeal_ids: list[int]) -> dict:
    """Missing = no evidence; None = a reply may be first but its time is unknown."""
    if not appeal_ids:
        return {}
    anchor = func.coalesce(Appeal.sla_anchor_at, Appeal.created_at)
    query = select(
        Message.appeal_id, Message.created_at, Message.delivery_source,
        Message.delivery_uncertain_until, anchor,
    ).join(Appeal, Appeal.id == Message.appeal_id).where(
        Message.appeal_id.in_(appeal_ids), Message.direction == "from_operator",
        or_(Message.created_at >= anchor, Message.delivery_uncertain_until >= anchor),
    )
    pending = select(
        OperatorReplyAttempt.appeal_id, OperatorReplyAttempt.created_at,
        literal("pending"), OperatorReplyAttempt.created_at, anchor,
    ).join(Appeal).where(OperatorReplyAttempt.appeal_id.in_(appeal_ids), pending_in_period(anchor))
    # One statement snapshot: reconciliation cannot disappear between two reads.
    rows = await session.stream(union_all(query, pending).execution_options(yield_per=500))
    exact: dict[int, datetime] = {}
    uncertain: dict[int, datetime] = {}
    async for appeal_id, created, source, until, period_start in rows:
        if until is not None or source == "manual":
            # Historical manual records store confirmation time, not a lower bound.
            lower = max(created, period_start) if until is not None else period_start
            uncertain[appeal_id] = min(uncertain.get(appeal_id, lower), lower)
        else:
            exact[appeal_id] = min(exact.get(appeal_id, created), created)
    result: dict[int, datetime | None] = dict(exact)
    for appeal_id, lower in uncertain.items():
        if appeal_id not in exact or lower <= exact[appeal_id]:
            result[appeal_id] = None
    return result


async def latest_intermediate_times(session, appeal_ids: list[int]) -> dict:
    if not appeal_ids:
        return {}
    anchor = func.coalesce(Appeal.sla_anchor_at, Appeal.created_at)
    query = select(
        Message.appeal_id, Message.id, Message.created_at, Message.delivery_source,
        Message.delivery_uncertain_until, Message.response_kind,
    ).join(Appeal).where(
        Message.appeal_id.in_(appeal_ids), Message.direction == "from_operator",
        or_(Message.created_at >= anchor, Message.delivery_uncertain_until >= anchor),
    )
    pending = select(
        OperatorReplyAttempt.appeal_id, OperatorReplyAttempt.id, OperatorReplyAttempt.created_at,
        literal("pending"), OperatorReplyAttempt.created_at, literal(None),
    ).join(Appeal).where(OperatorReplyAttempt.appeal_id.in_(appeal_ids), pending_in_period(anchor))
    rows = await session.stream(union_all(query, pending).execution_options(yield_per=500))
    exact: dict[int, tuple[datetime, int, str | None]] = {}
    uncertain: dict[int, datetime] = {}
    unresolved: set[int] = set()
    async for appeal_id, message_id, created, source, until, kind in rows:
        if source == "pending":
            unresolved.add(appeal_id)
            continue
        if until is not None or source == "manual":
            upper = until or created
            uncertain[appeal_id] = max(uncertain.get(appeal_id, upper), upper)
        else:
            candidate = (created, message_id, kind)
            if appeal_id not in exact or candidate[:2] > exact[appeal_id][:2]:
                exact[appeal_id] = candidate
    result: dict[int, datetime | None] = {
        appeal_id: value[0] for appeal_id, value in exact.items() if value[2] == "intermediate"
    }
    for appeal_id, upper in uncertain.items():
        if appeal_id not in exact or upper >= exact[appeal_id][0]:
            result[appeal_id] = None
    # Until reconciliation, an in-flight or timed-out attempt may still change
    # the latest-response timer even when the first response is already known.
    for appeal_id in unresolved:
        result[appeal_id] = None
    return result
