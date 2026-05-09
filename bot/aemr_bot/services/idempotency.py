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

    # Храним только summary события — без полного дампа, который содержал
    # бы phone, имя и текст обращения жителя. Полный payload жил бы в
    # таблице events 30 дней, попадал в backup и в логи восстановления —
    # это лишняя поверхность для ПДн. Для целей идемпотентности достаточно
    # знать тип, mid и таймстамп.
    payload: dict = {"summary_only": True}
    try:
        if hasattr(event, "model_dump"):
            full = event.model_dump(mode="json", exclude={"bot"})
            # Достаём только метаданные, не текст и вложения.
            payload = {
                "update_type": full.get("update_type"),
                "timestamp": full.get("timestamp"),
                "chat_id": (full.get("message", {}) or {}).get("recipient", {}).get("chat_id")
                            if isinstance(full.get("message"), dict) else None,
                "user_id": (full.get("message", {}) or {}).get("sender", {}).get("user_id")
                            if isinstance(full.get("message"), dict) else None,
                "mid": (full.get("message", {}) or {}).get("body", {}).get("mid")
                            if isinstance(full.get("message"), dict) else None,
            }
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


async def try_mark_processed_raw(key: str, kind: str) -> bool:
    """Простая обёртка над insert into events для произвольного ключа.

    Используется для дедупа, не связанного с MAX-Update — например,
    дедуп ответов оператора между процессами (см. operator_reply.
    _is_duplicate_reply_db). Возвращает True если ключ свободен (мы
    его заняли), False если уже занят (дубль).

    Хранится 30 дней по общему events-retention.
    """
    try:
        async with session_scope() as session:
            stmt = (
                pg_insert(Event)
                .values(
                    idempotency_key=key,
                    update_type=kind,
                    payload={"raw_dedup": True},
                )
                .on_conflict_do_nothing(index_elements=[Event.idempotency_key])
            )
            result = await session.execute(stmt)
            return (result.rowcount or 0) > 0
    except IntegrityError:
        return False
    except Exception:
        log.exception("raw idempotency claim failed; defaulting to process")
        return True
