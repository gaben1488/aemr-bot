"""Coordinate receipt execution with erasure through the erasure commit.

Single-process singleton-lock deployment. The same task may erase itself while
handling /forget, hence reentrancy is intentional. Weak storage keeps idle IDs out
of memory; holders/waiters/transaction callbacks keep their gate alive.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from weakref import WeakValueDictionary

from sqlalchemy import event


class _Gate:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.owner: asyncio.Task | None = None
        self.depth = 0

    async def acquire(self) -> None:
        task = asyncio.current_task()
        if self.owner is not task:
            await self.lock.acquire()
            self.owner = task
        self.depth += 1

    def release(self) -> None:
        self.depth -= 1
        if not self.depth:
            self.owner = None
            self.lock.release()


current_subject: ContextVar[int | None] = ContextVar("resident_current_subject", default=None)


_gates: WeakValueDictionary[int, _Gate] = WeakValueDictionary()


def _gate(user_id: int) -> _Gate:
    gate = _gates.get(user_id)
    if gate is None:
        gate = _Gate()
        _gates[user_id] = gate
    return gate


@asynccontextmanager
async def guard(user_id: int | None):
    if user_id is None:
        yield
        return
    gate = _gate(user_id)
    await gate.acquire()
    token = current_subject.set(user_id)
    try:
        yield
    finally:
        current_subject.reset(token)
        gate.release()


async def hold_until_commit(session, user_id: int) -> None:
    """Retain erasure's gate until its caller commits/rolls back the outer tx."""
    holds = session.info.setdefault("resident_erasure_gates", {})
    if user_id in holds:
        return
    gate = _gate(user_id)
    await gate.acquire()
    try:
        if not session.in_transaction():
            await session.begin()
        sync_session = session.sync_session
        loop = asyncio.get_running_loop()
        released = False

        def release_after_transaction(ended_session, transaction):
            nonlocal released
            if transaction.parent is not None or released:
                return
            released = True
            holds.pop(user_id, None)
            gate.release()
            # Do not mutate SQLAlchemy's listener deque while dispatching it.
            loop.call_soon(event.remove, sync_session, "after_transaction_end", release_after_transaction)

        event.listen(sync_session, "after_transaction_end", release_after_transaction)
        holds[user_id] = gate
    except BaseException:
        gate.release()
        raise
