"""Сервис подписки и рассылки новостей.

Подписчики — пользователи, давшие явное согласие через /subscribe или
кнопку «Подписаться на новости». Заблокированные (`is_blocked=true`) и
обезличенные (после /erase, `first_name='Удалено'`) автоматически
исключаются из списка получателей. См. `count_subscribers` и
`list_subscriber_targets`.
"""

from __future__ import annotations

from datetime import datetime, timezone

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
    """SQLAlchemy-выражение, отбирающее только тех, кому можно доставить."""
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


async def list_subscriber_targets(
    session: AsyncSession,
) -> list[tuple[int, int]]:
    """Снимок подходящих подписчиков как кортежей (db_id, max_user_id).

    Цикл отправки работает уже с обычными питоновскими данными, чтобы
    транзакция закрылась сразу. Если держать её открытой на всё время
    рассылки, это заблокирует VACUUM и накопит WAL при долгой рассылке.
    """
    result = await session.execute(
        select(User.id, User.max_user_id).where(_eligible_filter()).order_by(User.id)
    )
    return [(row[0], row[1]) for row in result.all()]


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
    """Перевести статус в «отменено». Возвращает True, если рассылка
    шла, и False, если она уже была в терминальном состоянии. По этому
    флагу вызывающий код понимает, что отмена ничего не изменила."""
    result = await session.execute(
        update(Broadcast)
        .where(
            Broadcast.id == broadcast_id,
            Broadcast.status == BroadcastStatus.SENDING.value,
        )
        .values(status=BroadcastStatus.CANCELLED.value)
    )
    return result.rowcount > 0


async def reap_orphaned_sending(session: AsyncSession) -> int:
    """При старте перевести каждую запись Broadcast.SENDING в FAILED.

    Запись со статусом SENDING на старте означает, что предыдущий процесс
    бота умер посреди рассылки (падение, OOM, остановка контейнера,
    перезагрузка хоста). Цикл отправки не дошёл до finally-блока с
    mark_finished, и запись осталась бы навсегда в SENDING. Это путает
    /broadcast list и блокирует оператора, который пробует запустить
    новую рассылку при «всё ещё идущей» старой.

    Поле `finished_at` остаётся NULL: точное время остановки рассылки
    неизвестно, а штамп времени запуска reaper тихо солгал бы в
    `/broadcast list` («закончено N секунд назад», хотя на самом деле
    процесс упал часы назад).

    Возвращает число переведённых записей для лога.
    """
    result = await session.execute(
        update(Broadcast)
        .where(Broadcast.status == BroadcastStatus.SENDING.value)
        .values(status=BroadcastStatus.FAILED.value)
    )
    return result.rowcount or 0


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
