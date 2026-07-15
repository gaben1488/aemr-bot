"""Single-instance гард через Postgres advisory-lock.

Два процесса бота с одним `BOT_TOKEN` молча делят long-poll апдейты MAX:
часть сообщений жителя обрабатывает «не тот» экземпляр, ломается состояние
анкет-воронок (FSM), возникают гонки. Раньше защита держалась только на
дисциплине оператора («не запускать второй экземпляр») и памятке — код её
не форсил. Авто-деплой на не-каноническом хосте способен тихо воскресить
второй экземпляр.

Механизм: на старте берём session-level advisory-lock по ключу, выведенному
из токена (разные боты → разные замки; два процесса ОДНОГО бота конфликтуют,
что и нужно). Держим соединение открытым всю жизнь процесса — его закрытие
или падение процесса снимает лок автоматически, поэтому гард переживает
рестарт БД (после переподключения новый процесс возьмёт лок заново) и не
оставляет «залипший» замок после аварии.

SQLite (unit-тесты) advisory-lock не поддерживает — там no-op (None).
"""
from __future__ import annotations

import hashlib
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aemr_bot.config import settings
from aemr_bot.db.session import engine as _default_engine

log = logging.getLogger(__name__)


class SingleInstanceError(RuntimeError):
    """Другой процесс уже держит single-instance lock для этого токена."""


def _lock_key(token: str) -> int:
    """Стабильный int64-ключ advisory-lock из токена.

    `hash()` нестабилен между процессами (PYTHONHASHSEED) — берём SHA-256,
    первые 8 байт как signed int64 (тип аргумента pg_advisory_lock — bigint).
    Токен, а не константа: разные боты на одной БД не блокируют друг друга,
    но два процесса одного бота — блокируют.
    """
    digest = hashlib.sha256(token.encode("utf-8")).digest()[:8]
    return int.from_bytes(digest, "big", signed=True)


async def acquire_single_instance_lock(
    eng: AsyncEngine | None = None,
) -> AsyncConnection | None:
    """Взять single-instance advisory-lock или бросить SingleInstanceError.

    Возвращает удерживающее соединение (НЕ закрывать — оно держит лок всю
    жизнь процесса) либо None, если БД не Postgres (sqlite no-op).
    """
    eng = eng or _default_engine
    if not str(eng.url).startswith("postgresql"):
        return None

    key = _lock_key(settings.bot_token)
    conn = await eng.connect()
    try:
        got = await conn.scalar(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": key}
        )
    except Exception:
        await conn.close()
        raise
    if not got:
        await conn.close()
        raise SingleInstanceError(
            "another process already holds the single-instance lock for this "
            "BOT_TOKEN — two instances on one token split MAX updates and "
            "corrupt wizard-funnel state; refusing to start. Check for a second "
            "container/systemd unit or a stray `compose up` on another host."
        )
    log.info("single-instance lock acquired (advisory key %d)", key)
    return conn
