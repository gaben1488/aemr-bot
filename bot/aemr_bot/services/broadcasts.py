"""Broadcast subscription and dispatch service.

Subscribers are users who explicitly opted in via /subscribe or the
«Подписаться на новости» button. Blocked users (`is_blocked=true`) and
anonymized users (after /erase, `first_name='Удалено'`) are excluded
from the recipient list automatically — see `count_subscribers` and
`iter_subscribers`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import AsyncIterator

from sqlalchemy import desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aemr_bot.db.models import Broadcast, BroadcastDelivery, BroadcastStatus, User


async def is_subscribed(session: AsyncSession, max_user_id: int) -> bool:
    user = await session.scalar(select(User).where(User.max_user_id == max_user_id))
    return bool(user and user.subscribed_broadcast)


async def set_subscription(
    session: AsyncSession, max_user_id: int, subscribed: bool
) -> None:
    await session.execute(
        update(User)
        .where(User.max_user_id == max_user_id)
        .values(subscribed_broadcast=subscribed)
    )


def _eligible_filter():
    """SQLAlchemy expression that selects only deliverable users."""
    return (
        (User.subscribed_broadcast.is_(True))
        & (User.is_blocked.is_(False))
        & (User.first_name != "Удалено")
    )


async def count_subscribers(session: AsyncSession) -> int:
    return (
        await session.scalar(
            select(func.count()).select_from(User).where(_eligible_filter())
        )
    ) or 0


async def iter_subscribers(session: AsyncSession) -> AsyncIterator[User]:
    """Yield eligible subscribers ordered by id (stable order across runs)."""
    result = await session.scalars(
        select(User).where(_eligible_filter()).order_by(User.id)
    )
    for user in result:
        yield user


async def create_broadcast(
    session: AsyncSession,
    *,
    text: str,
    operator_id: int | None,
    subscriber_count: int,
) -> Broadcast:
    bc = Broadcast(
        created_by_operator_id=operator_id,
        text=text,
        subscriber_count_at_start=subscriber_count,
        status=BroadcastStatus.DRAFT.value,
    )
    session.add(bc)
    await session.flush()
    return bc


async def mark_started(
    session: AsyncSession, broadcast_id: int, admin_message_id: str | None
) -> None:
    await session.execute(
        update(Broadcast)
        .where(Broadcast.id == broadcast_id)
        .values(
            status=BroadcastStatus.SENDING.value,
            started_at=datetime.now(timezone.utc),
            admin_message_id=admin_message_id,
        )
    )


async def mark_finished(
    session: AsyncSession,
    broadcast_id: int,
    *,
    status: BroadcastStatus,
    delivered: int,
    failed: int,
) -> None:
    await session.execute(
        update(Broadcast)
        .where(Broadcast.id == broadcast_id)
        .values(
            status=status.value,
            finished_at=datetime.now(timezone.utc),
            delivered_count=delivered,
            failed_count=failed,
        )
    )


async def request_cancel(session: AsyncSession, broadcast_id: int) -> bool:
    """Set status to cancelled. Returns True if the broadcast was sending,
    False if it was already in a terminal state — caller can use this to
    detect a no-op cancel."""
    result = await session.execute(
        update(Broadcast)
        .where(
            Broadcast.id == broadcast_id,
            Broadcast.status == BroadcastStatus.SENDING.value,
        )
        .values(status=BroadcastStatus.CANCELLED.value)
    )
    return result.rowcount > 0


async def get_status(session: AsyncSession, broadcast_id: int) -> str | None:
    return await session.scalar(
        select(Broadcast.status).where(Broadcast.id == broadcast_id)
    )


async def record_delivery(
    session: AsyncSession,
    *,
    broadcast_id: int,
    user_id: int,
    error: str | None,
) -> None:
    delivered_at = datetime.now(timezone.utc) if error is None else None
    session.add(
        BroadcastDelivery(
            broadcast_id=broadcast_id,
            user_id=user_id,
            delivered_at=delivered_at,
            error=error,
        )
    )
    await session.flush()


async def update_progress(
    session: AsyncSession,
    broadcast_id: int,
    *,
    delivered: int,
    failed: int,
) -> None:
    await session.execute(
        update(Broadcast)
        .where(Broadcast.id == broadcast_id)
        .values(delivered_count=delivered, failed_count=failed)
    )


async def list_recent(session: AsyncSession, limit: int = 10) -> list[Broadcast]:
    res = await session.scalars(
        select(Broadcast).order_by(desc(Broadcast.created_at)).limit(limit)
    )
    return list(res)


async def get_by_id(session: AsyncSession, broadcast_id: int) -> Broadcast | None:
    return await session.scalar(select(Broadcast).where(Broadcast.id == broadcast_id))
