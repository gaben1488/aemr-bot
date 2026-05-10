"""DB persistence для wizard state'а оператора.

Дополняет `services/wizard_registry`:
- registry: быстрый in-memory cache, primary read path для running-
  процесса.
- этот модуль: durable storage в Postgres, чтобы wizards переживали
  рестарт.

Workflow:
1. На старте бота `await hydrate_into_registry(session)` подгружает
   активные записи из БД в in-memory dict'ы registry.
2. Handler зовёт `wr.set_op_wizard(operator_id, state)` — обновляет
   in-memory СРАЗУ.
3. Тот же handler зовёт `await save_op_wizard(session, ...)` —
   обновляет БД (UPSERT).
4. На clear то же самое: `wr.clear_op_wizard()` + `await delete_op_wizard()`.

Если БД-вызов упадёт (network blip), in-memory остаётся правильным;
рестарт в этот момент потеряет state, но это не хуже чем раньше.
TTL: op-wizard — 5 минут, broadcast — 30 минут (мастера в админ-чате
не должны жить дольше).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from aemr_bot.db.models import WizardState
from aemr_bot.services import wizard_registry as wr

log = logging.getLogger(__name__)

KIND_OP = "op"
KIND_BROADCAST = "broadcast"

# TTL в секундах. Op-wizard короче — это узкое окно регистрации
# нового сотрудника. Broadcast длиннее — оператор может думать над
# текстом 10–20 минут.
TTL_OP_SEC = 5 * 60
TTL_BROADCAST_SEC = 30 * 60


def _ttl_for(kind: str) -> int:
    return TTL_OP_SEC if kind == KIND_OP else TTL_BROADCAST_SEC


async def _upsert(
    session: AsyncSession,
    kind: str,
    operator_max_user_id: int,
    state: dict[str, Any],
) -> None:
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=_ttl_for(kind))
    stmt = pg_insert(WizardState).values(
        kind=kind,
        operator_max_user_id=operator_max_user_id,
        state=state,
        expires_at=expires_at,
    )
    # ON CONFLICT (kind, operator_max_user_id) DO UPDATE — переписываем
    # state и expires_at; updated_at обновится через onupdate=func.now().
    stmt = stmt.on_conflict_do_update(
        constraint="uq_wizard_state_kind_operator",
        set_={
            "state": stmt.excluded.state,
            "expires_at": stmt.excluded.expires_at,
        },
    )
    await session.execute(stmt)


async def save_op_wizard(
    session: AsyncSession, operator_max_user_id: int, state: dict[str, Any]
) -> None:
    await _upsert(session, KIND_OP, operator_max_user_id, state)


async def save_broadcast_wizard(
    session: AsyncSession, operator_max_user_id: int, state: dict[str, Any]
) -> None:
    await _upsert(session, KIND_BROADCAST, operator_max_user_id, state)


async def delete_op_wizard(
    session: AsyncSession, operator_max_user_id: int
) -> None:
    await session.execute(
        delete(WizardState).where(
            WizardState.kind == KIND_OP,
            WizardState.operator_max_user_id == operator_max_user_id,
        )
    )


async def delete_broadcast_wizard(
    session: AsyncSession, operator_max_user_id: int
) -> None:
    await session.execute(
        delete(WizardState).where(
            WizardState.kind == KIND_BROADCAST,
            WizardState.operator_max_user_id == operator_max_user_id,
        )
    )


async def hydrate_into_registry(session: AsyncSession) -> tuple[int, int]:
    """Прочитать активные wizards из БД и положить в in-memory registry.

    Вызывается ОДИН РАЗ на старте бота, до приёма событий. Записи с
    expires_at в прошлом игнорируются и удаляются (lazy GC).

    Возвращает: (op_count, broadcast_count) — для лога старта.
    """
    now = datetime.now(timezone.utc)

    # GC просроченных
    deleted = await session.execute(
        delete(WizardState).where(WizardState.expires_at <= now)
    )
    if deleted.rowcount:
        log.info("wizard_persist: GC'd %d expired wizards", deleted.rowcount)

    rows = (await session.scalars(
        select(WizardState).where(WizardState.expires_at > now)
    )).all()

    op_count = 0
    bcast_count = 0
    for row in rows:
        if row.kind == KIND_OP:
            wr.set_op_wizard(row.operator_max_user_id, dict(row.state or {}))
            op_count += 1
        elif row.kind == KIND_BROADCAST:
            wr.set_broadcast_wizard(
                row.operator_max_user_id, dict(row.state or {})
            )
            bcast_count += 1
        else:
            log.warning(
                "wizard_persist: unknown kind=%r in DB row id=%s — skip",
                row.kind, row.id,
            )

    log.info(
        "wizard_persist: hydrated %d op-wizards, %d broadcast-wizards",
        op_count, bcast_count,
    )
    return op_count, bcast_count
