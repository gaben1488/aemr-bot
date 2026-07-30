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


# Статусы verify_single_instance_lock. Интервал проверки — константа,
# а не settings: это внутренний watchdog без операционной надобности
# его крутить.
LOCK_OK = "ok"
LOCK_REACQUIRED = "reacquired"
LOCK_WATCHDOG_INTERVAL_SECONDS = 60.0


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


async def verify_single_instance_lock(
    conn: AsyncConnection | None,
    eng: AsyncEngine | None = None,
) -> tuple[str, AsyncConnection | None]:
    """Проверить, что advisory-lock всё ещё наш; вернуть (status, conn).

    Session-level lock живёт на соединении: рестарт postgres или обрыв
    сети тихо снимает замок, а процесс продолжает работать так, будто
    держит его, — окно для второго экземпляра. Периодический вызов
    (lock-watchdog в main.py) закрывает окно:

    - соединение живо (`SELECT 1` прошёл) → замок держится:
      (LOCK_OK, прежний conn);
    - соединение мертво → переакквизиция свежим соединением удалась:
      (LOCK_REACQUIRED, новый conn) — держать его вместо старого;
    - соединение мертво и замок занят другим процессом →
      SingleInstanceError пробрасывается: вызывающий обязан завершить
      процесс (работать вторым экземпляром нельзя, systemd/compose
      перезапустит и рестарт встанет в обычную очередь за замком).

    Прочие исключения (БД целиком недоступна — ни проверить, ни
    переакквизировать) тоже пробрасываются: вызывающий решает, ждать ли
    следующего тика.

    conn=None (sqlite no-op режим) → всегда (LOCK_OK, None).
    """
    if conn is None:
        return LOCK_OK, None
    try:
        await conn.scalar(text("SELECT 1"))
        return LOCK_OK, conn
    except Exception:
        log.warning(
            "single-instance lock: соединение потеряно, замок снят БД — "
            "пробуем переакквизицию",
            exc_info=True,
        )
    try:
        await conn.close()
    except Exception:
        log.debug("stale lock conn close failed", exc_info=True)
    new_conn = await acquire_single_instance_lock(eng)
    return LOCK_REACQUIRED, new_conn


async def release_single_instance_lock(conn: AsyncConnection | None) -> None:
    """Снять single-instance lock и вернуть соединение в пул.

    ВАЖНО: просто `conn.close()` НЕ снимает advisory-lock — SQLAlchemy
    возвращает физическое соединение в пул живым, а session-level lock
    держится на живом бэкенде. Поэтому явно зовём pg_advisory_unlock на
    ТОМ ЖЕ соединении, потом закрываем. При падении процесса соединение
    рвётся и лок снимается сам — эта функция для штатного shutdown.
    """
    if conn is None:
        return
    try:
        await conn.execute(
            text("SELECT pg_advisory_unlock(:k)"),
            {"k": _lock_key(settings.bot_token)},
        )
    except Exception:
        log.debug("pg_advisory_unlock failed (закроем соединение)", exc_info=True)
    finally:
        try:
            await conn.close()
        except Exception:
            log.debug("single-instance lock conn close failed", exc_info=True)
