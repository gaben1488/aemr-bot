"""Build retention candidate selection from operational activity."""
from sqlalchemy import case, func, or_, select

from aemr_bot.db.models import Appeal, Message, OperatorReplyAttempt


def old_content_candidates(threshold):
    completed_or_created = func.coalesce(Appeal.closed_at, Appeal.answered_at, Appeal.created_at)
    period_started = func.coalesce(Appeal.sla_anchor_at, Appeal.created_at)
    work_started = func.coalesce(Appeal.work_started_at, Appeal.created_at)
    # A reopened case can have old creation/completion timestamps but a
    # brand-new operational period. Taking NEW into work is activity too;
    # keep that clock separate so it never postpones the first-response SLA.
    latest_period = case((period_started > completed_or_created, period_started), else_=completed_or_created)
    retention_anchor = case((work_started > latest_period, work_started), else_=latest_period)
    return select(Appeal.id).where(
        retention_anchor.isnot(None),
        retention_anchor <= threshold,
        ~select(Message.id).where(
            Message.appeal_id == Appeal.id,
            or_(Message.created_at > threshold, Message.delivery_uncertain_until > threshold),
        ).exists(),
        ~select(OperatorReplyAttempt.id).where(
            OperatorReplyAttempt.appeal_id == Appeal.id,
            OperatorReplyAttempt.state.in_(["sending", "unknown", "confirmed", "not_delivered", "recorded"]),
            func.coalesce(OperatorReplyAttempt.last_activity_at, OperatorReplyAttempt.created_at) > threshold,
        ).exists(),
    )
