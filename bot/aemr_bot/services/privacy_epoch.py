"""Serialize export delivery with erasure without storing resident identifiers."""
import asyncio
import weakref
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import SessionTransaction

from aemr_bot.db.models import PrivacyEpoch

_LOCK_KEY = 0x41454D5250444E


@dataclass(frozen=True)
class _DeliveryHold:
    owner: weakref.ReferenceType[asyncio.Task[Any]]
    transaction: weakref.ReferenceType[SessionTransaction]


_delivery_holds: ContextVar[tuple[_DeliveryHold, ...]] = ContextVar("privacy_delivery_holds", default=())


async def read(session) -> int:
    value = await session.scalar(select(PrivacyEpoch.generation).where(PrivacyEpoch.id == 1))
    return int(value or 0)


async def mark_erasure(session) -> None:
    """Exclusive transaction lock covers deletion through its commit."""
    if session.bind.dialect.name == "postgresql":
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY})
    statement = insert(PrivacyEpoch).values(id=1, generation=1).on_conflict_do_update(
        index_elements=[PrivacyEpoch.id], set_={"generation": PrivacyEpoch.generation + 1}
    )
    await session.execute(statement)


async def hold_delivery(session) -> None:
    """Block erasure through delivery; same-task nested readers borrow its lock.

    A queued exclusive eraser blocks new readers, including a second connection
    opened by the task already holding the first reader. Borrowing that live
    outer transaction avoids an application deadlock. Awaited nested delivery
    must finish before its protecting outer scope exits. Child tasks always
    acquire their own lock: copied context does not extend a parent's lifetime.
    Ended transactions are discarded, including when a session is reused.
    """
    if session.bind.dialect.name != "postgresql":
        return
    owner = asyncio.current_task()
    if owner is None:
        raise RuntimeError("Delivery barrier requires an asyncio task")
    live = tuple(hold for hold in _delivery_holds.get()
                 if hold.owner() is owner
                 and (transaction := hold.transaction()) is not None
                 and transaction.is_active
                 and transaction.session.get_transaction() is transaction)
    _delivery_holds.set(live)
    if live:
        return
    await session.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": _LOCK_KEY})
    transaction = session.sync_session.get_transaction()
    if transaction is None:
        raise RuntimeError("Delivery lock has no protecting transaction")
    _delivery_holds.set((*live, _DeliveryHold(weakref.ref(owner), weakref.ref(transaction))))


async def hold_export_if_current(session, expected: int) -> bool:
    """Hold a shared lock through file upload/send, only for a fresh snapshot."""
    await hold_delivery(session)
    return await read(session) == expected
