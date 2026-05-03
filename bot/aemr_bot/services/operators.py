from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aemr_bot.db.models import AuditLog, Operator, OperatorRole


async def get(session: AsyncSession, max_user_id: int) -> Operator | None:
    return await session.scalar(
        select(Operator).where(Operator.max_user_id == max_user_id, Operator.is_active.is_(True))
    )


async def is_operator(session: AsyncSession, max_user_id: int) -> bool:
    return await get(session, max_user_id) is not None


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
    """Cold-start the first IT operator from env, if no IT exists yet.

    Idempotent: returns True if a row was inserted/updated, False if an
    active IT was already present (no-op). Lets `BOOTSTRAP_IT_MAX_USER_ID`
    replace the manual psql INSERT step from RUNBOOK §6.1.
    """
    if await has_any_it(session):
        return False
    await upsert(
        session,
        max_user_id=max_user_id,
        full_name=full_name,
        role=OperatorRole.IT,
    )
    return True
