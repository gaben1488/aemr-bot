from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aemr_bot.db.models import AuditLog, Operator, OperatorRole


async def get(session: AsyncSession, max_user_id: int) -> Operator | None:
    """Активный оператор по max_user_id. Историю не возвращает —
    деактивированных не видно."""
    return await session.scalar(
        select(Operator).where(Operator.max_user_id == max_user_id, Operator.is_active.is_(True))
    )


async def get_any(session: AsyncSession, max_user_id: int) -> Operator | None:
    """Любая запись по max_user_id, включая деактивированную. Нужен для
    /add из карточки — чтобы понять «человек уже был, надо реактивировать,
    а не вставлять новую строку»."""
    return await session.scalar(
        select(Operator).where(Operator.max_user_id == max_user_id)
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


async def deactivate(session: AsyncSession, max_user_id: int) -> Operator | None:
    """Мягкое удаление: is_active=false. Возвращает обновлённую запись
    или None, если активного оператора с таким max_user_id не было.

    Физическое DELETE не делается — у appeals.assigned_operator_id и
    messages.operator_id есть FK на operators с ON DELETE SET NULL,
    но потеря истории «кто отвечал жителю» нарушит требование
    журналирования по 152-ФЗ. Поэтому только деактивация.

    Запись остаётся в БД и может быть реактивирована через upsert()
    с той же max_user_id — это и есть «добавить повторно».
    """
    op = await session.scalar(
        select(Operator).where(
            Operator.max_user_id == max_user_id,
            Operator.is_active.is_(True),
        )
    )
    if op is None:
        return None
    op.is_active = False
    await session.flush()
    return op


async def change_role(
    session: AsyncSession,
    max_user_id: int,
    role: OperatorRole,
) -> Operator | None:
    """Сменить роль активному оператору без перевода через wizard.
    Возвращает обновлённую запись или None."""
    op = await session.scalar(
        select(Operator).where(
            Operator.max_user_id == max_user_id,
            Operator.is_active.is_(True),
        )
    )
    if op is None:
        return None
    op.role = role.value
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


async def list_all(session: AsyncSession) -> list[Operator]:
    """Все операторы, включая деактивированных. Нужен для расширенного
    экрана управления, чтобы IT-админ видел кого можно реактивировать."""
    res = await session.scalars(
        select(Operator).order_by(
            Operator.is_active.desc(), Operator.role, Operator.full_name
        )
    )
    return list(res)


async def count_active_by_role(
    session: AsyncSession, role: OperatorRole
) -> int:
    """Сколько активных операторов в роли. Используется для защиты от
    деактивации единственного IT-оператора."""
    from sqlalchemy import func as _func

    val = await session.scalar(
        select(_func.count(Operator.id)).where(
            Operator.role == role.value,
            Operator.is_active.is_(True),
        )
    )
    return int(val or 0)


async def has_any_it(session: AsyncSession) -> bool:
    op = await session.scalar(
        select(Operator).where(
            Operator.role == OperatorRole.IT.value, Operator.is_active.is_(True)
        )
    )
    return op is not None


async def cleanup_stale_operators(
    session: AsyncSession,
    *,
    current_member_ids: set[int],
    protected_role: OperatorRole = OperatorRole.IT,
) -> list[Operator]:
    """Деактивировать операторов, которых больше нет в админ-группе MAX.

    SECURITY_REVIEW M2 (max-threats CVE-9): когда оператор покидает
    служебную группу MAX (увольнение / смена должности / просто вышел
    «случайно»), запись в `operators` остаётся `is_active=true`. Auth
    защищён `is_admin_chat`-проверкой (на каждый callback re-проверка),
    но stale-данные в БД — повод для путаницы при ревизии и риск, если
    в будущем какая-то логика начнёт пускать по `is_active` без chat-
    binding.

    Источник истины — `current_member_ids`: множество `max_user_id` всех
    членов админ-группы, полученное через `bot.get_chat_members`. Если
    активный оператор НЕ в этом множестве — деактивируем (мягко, через
    `deactivate()`).

    **Защита от ошибки** (важно): если `current_member_ids` пуст (MAX
    API дал ошибку и `_safe_get_chat_members` вернул пустоту) — НЕ
    деактивируем никого, иначе одна сетевая флуктуация деактивирует
    всех операторов разом. Возвращаем пустой список.

    **Защита от self-lock-out**: операторов с ролью `protected_role`
    (по умолчанию IT) НЕ деактивируем автоматически — иначе если IT
    случайно вышел, никто потом не сможет его реактивировать. IT
    остаётся в operators, чтобы при возвращении он по-прежнему мог
    использовать админ-чат.

    Возвращает список деактивированных операторов (для audit и
    admin-alert).
    """
    if not current_member_ids:
        return []

    actives = await list_active(session)
    deactivated: list[Operator] = []
    for op in actives:
        if op.role == protected_role.value:
            continue
        if op.max_user_id in current_member_ids:
            continue
        # Оператор активен в БД, но не в группе MAX → деактивировать.
        result = await deactivate(session, op.max_user_id)
        if result is not None:
            deactivated.append(result)
            await write_audit(
                session,
                operator_max_user_id=None,  # системное действие
                action="operator_auto_deactivated_stale",
                target=str(op.max_user_id),
                details={
                    "reason": "left_admin_chat",
                    "role": op.role,
                    "full_name": op.full_name,
                },
            )
    return deactivated


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
