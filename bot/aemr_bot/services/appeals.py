from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from aemr_bot.db.models import Appeal, AppealStatus, Message, MessageDirection, User


async def create_appeal(
    session: AsyncSession,
    user: User,
    address: str,
    topic: str,
    summary: str,
    attachments: list,
    locality: str | None = None,
) -> Appeal:
    appeal = Appeal(
        user_id=user.id,
        status=AppealStatus.NEW.value,
        locality=locality,
        address=address,
        topic=topic,
        summary=summary,
        attachments=attachments,
    )
    session.add(appeal)
    await session.flush()
    return appeal


async def add_user_message(
    session: AsyncSession,
    appeal: Appeal,
    text: str | None,
    attachments: list | None = None,
    max_message_id: str | None = None,
) -> Message:
    msg = Message(
        appeal_id=appeal.id,
        direction=MessageDirection.FROM_USER.value,
        text=text,
        attachments=attachments or [],
        max_message_id=max_message_id,
    )
    session.add(msg)
    await session.flush()
    return msg


async def add_operator_message(
    session: AsyncSession,
    appeal: Appeal,
    text: str,
    operator_id: int | None,
    max_message_id: str | None,
) -> Message:
    msg = Message(
        appeal_id=appeal.id,
        direction=MessageDirection.FROM_OPERATOR.value,
        text=text,
        max_message_id=max_message_id,
        operator_id=operator_id,
    )
    session.add(msg)
    appeal.status = AppealStatus.ANSWERED.value
    appeal.answered_at = datetime.now(timezone.utc)
    if operator_id:
        appeal.assigned_operator_id = operator_id
    await session.flush()
    return msg


async def get_by_id(session: AsyncSession, appeal_id: int) -> Appeal | None:
    return await session.scalar(
        select(Appeal).options(selectinload(Appeal.user)).where(Appeal.id == appeal_id)
    )


async def get_by_admin_message_id(session: AsyncSession, admin_message_id: str) -> Appeal | None:
    return await session.scalar(
        select(Appeal)
        .options(selectinload(Appeal.user))
        .where(Appeal.admin_message_id == admin_message_id)
    )


async def list_for_user(
    session: AsyncSession,
    user_id: int,
    limit: int = 20,
    offset: int = 0,
) -> list[Appeal]:
    res = await session.scalars(
        select(Appeal)
        .where(Appeal.user_id == user_id)
        .order_by(desc(Appeal.created_at))
        .limit(limit)
        .offset(offset)
    )
    return list(res)


async def count_for_user(session: AsyncSession, user_id: int) -> int:
    return (
        await session.scalar(
            select(func.count()).select_from(Appeal).where(Appeal.user_id == user_id)
        )
    ) or 0


async def set_admin_message_id(session: AsyncSession, appeal_id: int, mid: str) -> None:
    await session.execute(
        update(Appeal).where(Appeal.id == appeal_id).values(admin_message_id=mid)
    )


async def reopen(session: AsyncSession, appeal_id: int) -> bool:
    result = await session.execute(
        update(Appeal)
        .where(Appeal.id == appeal_id)
        .values(status=AppealStatus.IN_PROGRESS.value, answered_at=None, closed_at=None)
    )
    return result.rowcount > 0


async def close(session: AsyncSession, appeal_id: int) -> bool:
    result = await session.execute(
        update(Appeal)
        .where(Appeal.id == appeal_id)
        .values(status=AppealStatus.CLOSED.value, closed_at=datetime.now(timezone.utc))
    )
    return result.rowcount > 0


async def find_overdue_unanswered(
    session: AsyncSession, sla_hours: int
) -> list[Appeal]:
    """Обращения, которые в работе/новые дольше SLA и пока без ответа.

    Используется часовым SLA-алёртом в `services/cron.py::sla_overdue_check`:
    раз в час смотрим, что висит дольше `sla_response_hours`, и шлём
    оператору список просроченных. Если ничего не висит — алерта нет
    (тишина в группе ценнее, чем «по нулям» каждый час).

    Возвращает результат с догруженным `user`, чтобы вызывающий код
    мог показать имя жителя без N+1.
    """
    threshold = datetime.now(timezone.utc) - timedelta(hours=sla_hours)
    res = await session.scalars(
        select(Appeal)
        .options(selectinload(Appeal.user))
        .where(
            Appeal.status.in_(
                [AppealStatus.NEW.value, AppealStatus.IN_PROGRESS.value]
            ),
            Appeal.created_at <= threshold,
        )
        .order_by(Appeal.created_at)
    )
    return list(res)


async def count_open(session: AsyncSession) -> int:
    """Сколько обращений висит без ответа (NEW + IN_PROGRESS).

    Используется в счётчике на кнопке «📋 Открытые обращения» в
    меню оператора, чтобы координатор сразу видел нагрузку, не нажимая.
    """
    return (
        await session.scalar(
            select(func.count())
            .select_from(Appeal)
            .where(
                Appeal.status.in_(
                    [AppealStatus.NEW.value, AppealStatus.IN_PROGRESS.value]
                )
            )
        )
    ) or 0


async def find_active_for_user(session: AsyncSession, user_id: int) -> Appeal | None:
    """Последнее живое обращение жителя.

    «Живое» = не закрытое окончательно. Сюда попадают обращения
    в статусах NEW (только что создано), IN_PROGRESS (оператор взял
    в работу) и ANSWERED (ответ отправлен, но житель ещё может
    написать «спасибо, но ещё одно» — это переоткроет обращение
    через handle_user_followup).

    На NEW и IN_PROGRESS тоже — чтобы житель мог дослать фото или
    уточнение к свежему обращению, не создавая новое. Сценарий «забыл
    приложить фото, через минуту вспомнил» базовый, и без этого
    дополнительные сообщения улетали в общий обработчик «не понимаю».
    """
    return await session.scalar(
        select(Appeal)
        .where(
            Appeal.user_id == user_id,
            Appeal.status.in_(
                [
                    AppealStatus.NEW.value,
                    AppealStatus.IN_PROGRESS.value,
                    AppealStatus.ANSWERED.value,
                ]
            ),
        )
        .order_by(desc(Appeal.created_at))
    )
