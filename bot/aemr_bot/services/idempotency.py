"""Persist a fingerprint of every incoming MAX update for idempotent dispatch.

In long-polling the MAX server occasionally redelivers updates after a network
hiccup. In webhook mode duplicates are routine — MAX retries on any non-2xx,
and asymmetric network errors mean we sometimes process the same payload twice.

Strategy: build a stable key per update, INSERT it into events with a unique
constraint, and short-circuit if the row already exists.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from aemr_bot.db.models import Event
from aemr_bot.db.session import session_scope

log = logging.getLogger(__name__)

MAX_KEY_LENGTH = 255


def build_idempotency_key(event: Any) -> str | None:
    """Compose a unique key from event fields. Returns None when nothing usable
    is available (rare; we'd rather process such an event than drop it)."""
    update_type = (
        getattr(event, "update_type", None)
        or getattr(getattr(event, "__class__", None), "__name__", "unknown")
    )
    update_type = str(update_type)

    parts: list[str] = [update_type]

    cb = getattr(event, "callback", None)
    if cb is not None:
        cb_id = getattr(cb, "callback_id", None)
        if cb_id:
            parts.append(f"cb={cb_id}")

    msg = getattr(event, "message", None)
    body = getattr(msg, "body", None) if msg is not None else None
    if body is not None:
        mid = getattr(body, "mid", None)
        if mid:
            parts.append(f"mid={mid}")
        seq = getattr(body, "seq", None)
        if seq is not None:
            parts.append(f"seq={seq}")

    timestamp = getattr(event, "timestamp", None)
    if timestamp is None and msg is not None:
        timestamp = getattr(msg, "timestamp", None)
    if timestamp is not None:
        parts.append(f"ts={timestamp}")

    chat_id = getattr(event, "chat_id", None)
    user = getattr(event, "user", None)
    user_id = getattr(user, "user_id", None) if user is not None else None
    if chat_id is not None:
        parts.append(f"chat={chat_id}")
    if user_id is not None:
        parts.append(f"user={user_id}")

    if len(parts) <= 1:
        return None

    key = "|".join(parts)
    if len(key) > MAX_KEY_LENGTH:
        key = key[:MAX_KEY_LENGTH]
    return key


async def claim(event: Any) -> bool:
    """Attempt to claim the event for processing. Returns True if this is the
    first time we see it (proceed), False if it's a duplicate (skip)."""
    key = build_idempotency_key(event)
    if key is None:
        return True

    update_type = str(
        getattr(event, "update_type", None)
        or getattr(getattr(event, "__class__", None), "__name__", "unknown")
    )

    payload: dict = {}
    try:
        if hasattr(event, "model_dump"):
            payload = event.model_dump(mode="json", exclude={"bot"})
    except Exception:
        payload = {"_serialization_error": True}

    try:
        async with session_scope() as session:
            stmt = (
                pg_insert(Event)
                .values(idempotency_key=key, update_type=update_type, payload=payload)
                .on_conflict_do_nothing(index_elements=[Event.idempotency_key])
            )
            result = await session.execute(stmt)
            if result.rowcount == 0:
                return False
        return True
    except IntegrityError:
        return False
    except Exception:
        log.exception("idempotency claim failed; defaulting to process")
        return True
