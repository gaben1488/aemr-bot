from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aemr_bot.db.models import AuditLog, Operator, OperatorRole


async def get(session: AsyncSession, max_user_id: int) -> Operator | None:
    return await session.scalar(
        select(Operator).where(Operator.max_user_id == max_user_id, Operator.is_active.is_(True))
    )


async def upsert(
    session: AsyncSession,
    max_user_id: int,
    full_name: str,
    role: OperatorRole,
) -> Operator:
    op = await session.scalar(select(Operator).where(Operator.max_user_id == max_user_id))
    if op is None:
        op = Operator(max_user_id=max_user_id, full_name=full_name, role=role.value)
        session.add(op)
    else:
        op.full_name = full_name
        op.role = role.value
        op.is_active = True
    await session.flush()
    return op


async def write_audit(
    session: AsyncSession,
    operator_max_user_id: int | None,
    action: str,
    target: str | None = None,
    details: dict | None = None,
) -> None:
    session.add(
        AuditLog(
            operator_max_user_id=operator_max_user_id,
            action=action,
            target=target,
            details=details,
        )
    )
    await session.flush()


async def list_active(session: AsyncSession) -> list[Operator]:
    """Все активные операторы — для списка «👥 Список» в меню IT."""
    res = await session.scalars(
        select(Operator).where(Operator.is_active.is_(True)).order_by(Operator.role, Operator.full_name)
    )
    return list(res)


async def has_any_it(session: AsyncSession) -> bool:
    op = await session.scalar(
        select(Operator).where(
            Operator.role == OperatorRole.IT.value, Operator.is_active.is_(True)
        )
    )
    return op is not None


async def bootstrap_it_from_env(
    session: AsyncSession,
    *,
    max_user_id: int,
    full_name: str,
) -> bool:
    """Холодный старт первого IT-оператора из переменной окружения,
    если активного IT ещё нет.

    Идемпотентно: возвращает True, если запись была вставлена или
    обновлена, и False, если активный IT уже существует (тогда ничего
    не делает). Позволяет `BOOTSTRAP_IT_MAX_USER_ID` заменить ручной
    шаг INSERT через psql из RUNBOOK §6.1.

    Advisory lock защищает от гонки при параллельном старте двух
    процессов: оба видят пустую таблицу, оба пытаются вставить — без
    lock'а получили бы две IT-записи (если bootstrap_it_max_user_id
    различался по env). Lock-ID 0xAE57B07 — фиксированный, чтобы не
    пересекался с приложенческими advisory-locks.
    """
    from sqlalchemy import text as sql_text

    await session.execute(sql_text("SELECT pg_advisory_xact_lock(:lid)"), {"lid": 0xAE57B07})
    if await has_any_it(session):
        return False
    await upsert(
        session,
        max_user_id=max_user_id,
        full_name=full_name,
        role=OperatorRole.IT,
    )
    return True
