"""Хранить отпечаток каждого входящего обновления MAX для защиты от
повторов (idempotency) при диспетчеризации.

В режиме long-polling сервер MAX иногда повторно доставляет обновления
после сетевого сбоя. В режиме webhook дубли — обычное дело: MAX
повторяет любую не-2xx, а несимметричные сетевые ошибки приводят к
двойной обработке одного и того же payload.

Стратегия: построить устойчивый ключ для обновления, выполнить INSERT в
events с уникальным ограничением и не делать ничего, если запись уже
существует.
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
    """Собрать уникальный ключ из полей события. Возвращает None, когда
    подходящих полей нет (редко, и в этом случае лучше обработать
    событие, чем выкинуть его)."""
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
    """Попытаться застолбить событие для обработки. Возвращает True,
    если видим его впервые (можно обрабатывать), и False, если это дубль
    (пропустить)."""
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
