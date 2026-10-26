"""Durable transport receipts. Never replay an operation with unknown side effects.

Pending payloads live at most 24 hours; done/failed payloads are removed immediately.
Only metadata remains for 30-day deduplication and operator diagnostics.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert

from aemr_bot.db.models import InboxEvent
from aemr_bot.db.session import session_scope

log = logging.getLogger(__name__)


@dataclass
class Outcome:
    finished: bool = False
    failed: bool = False
    retry_safe: bool = False
    reason: str | None = None


current_outcome: ContextVar[Outcome | None] = ContextVar("inbox_outcome", default=None)


def _identity(payload: dict) -> tuple[str, int | None]:
    def obj(value):
        return value if isinstance(value, dict) else {}
    msg = obj(payload.get("message"))
    callback = obj(payload.get("callback"))
    user = obj(callback.get("user")) or obj(payload.get("user")) or obj(msg.get("sender"))
    user_id = user.get("user_id")
    body = obj(msg.get("body"))
    # MAX IDs, not display text/name or JSON key order, define a duplicate.
    identity: Any
    if callback.get("callback_id"):
        identity = [payload.get("update_type"), callback["callback_id"]]
    elif body.get("mid"):
        identity = [payload.get("update_type"), body["mid"], body.get("seq"),
                    payload.get("timestamp", msg.get("timestamp")), user_id]
    else:
        identity = payload
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest(), user_id if isinstance(user_id, int) else None


async def accept(events: list[dict]) -> None:
    """Commit the whole batch before acknowledging it to MAX. Errors propagate."""
    if not events:
        return
    async with session_scope() as session:
        for payload in events:
            key, user_id = _identity(payload)
            await session.execute(insert(InboxEvent).values(
                event_key=key, update_type=str(payload.get("update_type", "unknown"))[:64],
                max_user_id=user_id, payload=payload,
            ).on_conflict_do_nothing(index_elements=[InboxEvent.event_key]))


async def recover_interrupted() -> None:
    """At a serial worker boundary, classify any unfinished previous execution."""
    async with session_scope() as session:
        await session.execute(update(InboxEvent).where(InboxEvent.state == "processing").values(
            state="failed", payload={}, reason="interrupted_unknown_result", updated_at=datetime.now(UTC),
        ))


async def take_next() -> tuple[int, dict] | None:
    async with session_scope() as session:
        row = await session.scalar(select(InboxEvent).where(InboxEvent.state == "pending")
                                   .order_by(InboxEvent.id).limit(1).with_for_update(skip_locked=True))
        if row is None:
            return None
        if row.received_at.replace(tzinfo=UTC) < datetime.now(UTC) - timedelta(hours=24):
            row.state, row.reason, row.payload = "failed", "expired_before_execution", {}
            row.updated_at = datetime.now(UTC)
            return None
        row.state = "processing"
        row.updated_at = datetime.now(UTC)
        return row.id, row.payload


async def finish(event_id: int, outcome: Outcome) -> None:
    async with session_scope() as session:
        values: dict[str, Any] = {"updated_at": datetime.now(UTC), "reason": outcome.reason}
        if outcome.retry_safe:
            values["state"] = "pending"
        else:
            values.update(state="failed" if outcome.failed else "done", payload={})
        # Erasure may have cancelled a pending/active receipt. Never restore its payload.
        await session.execute(update(InboxEvent).where(
            InboxEvent.id == event_id, InboxEvent.state == "processing",
        ).values(**values))


async def erase_pending(session, max_user_id: int) -> None:
    """Erase receipt bodies in the same transaction as resident data."""
    await session.execute(update(InboxEvent).where(InboxEvent.max_user_id == max_user_id).values(
        state="done", payload={}, max_user_id=None, reason="erased", updated_at=datetime.now(UTC),
    ))


async def maintenance(bot) -> None:
    from aemr_bot.services import admin_bus
    now = datetime.now(UTC)
    async with session_scope() as session:
        await session.execute(update(InboxEvent).where(
            InboxEvent.state == "pending", InboxEvent.received_at < now - timedelta(hours=24),
        ).values(state="failed", payload={}, reason="expired_before_execution", updated_at=now))
        await session.execute(delete(InboxEvent).where(InboxEvent.received_at < now - timedelta(days=30)))
        failures = list((await session.scalars(select(InboxEvent).where(
            InboxEvent.state == "failed", InboxEvent.notified.is_(False),
        ).order_by(InboxEvent.id).limit(20))).all())
    if not failures:
        return
    ids = ", ".join(f"#{r.id} ({r.update_type}, MAX ID {r.max_user_id or '—'})" for r in failures)
    sent = await admin_bus.send(bot, text=(
        "⚠️ Не завершена обработка событий: " + ids + ". "
        "Автоматического повтора нет: часть действий могла выполниться. "
        "Проверьте обращения и журнал рассылок перед повтором действия; "
        "при необходимости попросите отправителя повторить сообщение."
    ))
    if sent:
        async with session_scope() as session:
            await session.execute(update(InboxEvent).where(InboxEvent.id.in_([r.id for r in failures]))
                                  .values(notified=True))


async def process_one(dp, bot) -> bool:
    """Run one durable receipt. SDK swallows handler exceptions; middleware records them."""
    item = await take_next()
    if item is None:
        return False
    event_id, payload = item
    from aemr_bot.utils.resident_lifecycle import guard
    _, user_id = _identity(payload)
    async with guard(user_id):
        async with session_scope() as session:
            state = await session.scalar(select(InboxEvent.state).where(InboxEvent.id == event_id))
        if state != "processing":
            return True
        return await _dispatch_claimed(dp, bot, event_id, payload)


async def _dispatch_claimed(dp, bot, event_id: int, payload: dict) -> bool:
    from maxapi.methods.types.getted_updates import process_update_webhook
    outcome = Outcome()
    token = current_outcome.set(outcome)
    try:
        try:
            event = await process_update_webhook(event_json=payload, bot=bot)
            if event is None:
                outcome.failed, outcome.reason = True, "unsupported_event"
            else:
                async with asyncio.timeout(120):
                    await dp.handle(event)
                if not outcome.finished and not outcome.failed:
                    outcome.failed, outcome.reason = True, "dispatch_failed_before_handler"
        except asyncio.CancelledError:
            # Persisted processing state is deliberately NOT returned to pending.
            raise
        except Exception as exc:
            outcome.failed, outcome.reason = True, type(exc).__name__[:64]
        await finish(event_id, outcome)
        if outcome.retry_safe:
            await asyncio.sleep(2)
    finally:
        current_outcome.reset(token)
    return True


async def run(dp, bot, stop: asyncio.Event) -> None:
    """Serial dispatch preserves resident event order; ingestion remains independent."""
    next_maintenance = 0.0
    while not stop.is_set():
        try:
            await recover_interrupted()
            if asyncio.get_running_loop().time() >= next_maintenance:
                await maintenance(bot)
                next_maintenance = asyncio.get_running_loop().time() + 60
            worked = await process_one(dp, bot)
            if worked:
                continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never log payloads, SQL parameters or SDK exception representations.
            log.error("inbox iteration failed (%s)", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
        except TimeoutError:
            pass
