"""Маленький aiohttp-сервер здоровья процесса.

Контуры разделены намеренно:

* ``/livez`` — liveness. Возвращает 200, если жив asyncio-loop бота
  и heartbeat свежий. Не трогает БД. Этот endpoint используют Docker
  healthcheck, внешняя watchdog-перезапускалка и auto-deploy health-gate.
  Иначе краткая проблема Postgres превращалась в рестарт/rollback
  заведомо живого процесса.
* ``/readyz`` — readiness. Возвращает 200, если heartbeat свежий и БД
  отвечает на ``SELECT 1``. Это endpoint для диагностики и будущего
  балансировщика/webhook-readiness, но не для автоматического рестарта.
* ``/healthz`` оставлен как backward-compatible alias ``/readyz`` для
  ручных проверок и старых runbook-команд.

В режиме self-host с long-polling порт слушает 127.0.0.1 на хосте через
Docker port binding. Операционная детализация отдаётся только локальным
клиентам; внешним — только ``{"ok": ...}``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from aiohttp import web

from aemr_bot.config import settings as cfg

log = logging.getLogger(__name__)


@dataclass
class Heartbeat:
    last_beat: float = 0.0

    def beat(self) -> None:
        self.last_beat = time.monotonic()

    def is_fresh(self, max_age: float | None = None) -> bool:
        if self.last_beat == 0.0:
            return False
        if max_age is None:
            max_age = cfg.healthcheck_stale_seconds
        return (time.monotonic() - self.last_beat) <= max_age


heartbeat = Heartbeat()


async def _ping_db() -> bool:
    """SELECT 1 на живом движке. Возвращает False при любой ошибке.

    Бот с зависшим соединением к БД может крутить asyncio-цикл (и пульс
    останется зелёным), пока каждая операция, которая реально лезет в
    данные, виснет. Поэтому DB-ping нужен для readiness, но не должен
    управлять liveness/restart-политикой контейнера.
    """
    from sqlalchemy import text

    from aemr_bot.db.session import session_scope

    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        log.warning("readyz: DB ping failed", exc_info=True)
        return False


# Кэшируем результат ping'а БД на короткий интервал, чтобы плотная серия
# readiness-проверок не превращалась в шквал тривиальных SELECT'ов,
# конкурирующих с настоящими обработчиками за маленький пул соединений.
_DB_PING_CACHE_TTL = 10.0
_db_ping_cache: dict[str, float | bool] = {"value": False, "checked_at": 0.0}


async def _ping_db_cached() -> bool:
    now = time.monotonic()
    if now - float(_db_ping_cache["checked_at"]) < _DB_PING_CACHE_TTL:
        return bool(_db_ping_cache["value"])
    ok = await _ping_db()
    _db_ping_cache["value"] = ok
    _db_ping_cache["checked_at"] = now
    return ok


def _is_local_request(request: web.Request) -> bool:
    remote = request.remote or ""
    return remote in ("127.0.0.1", "::1", "localhost", "")


def _last_beat_age_seconds() -> float | None:
    if heartbeat.last_beat == 0.0:
        return None
    return round(time.monotonic() - heartbeat.last_beat, 1)


async def _status_response(request: web.Request, *, include_db: bool) -> web.Response:
    fresh = heartbeat.is_fresh()
    db_ok = await _ping_db_cached() if include_db else None
    ok = fresh and (bool(db_ok) if include_db else True)

    # Полная диагностика только локальным запросам — Docker healthcheck,
    # watchdog и ручная проверка на сервере. Внешним клиентам не отдаём
    # operational-информацию о БД и возрасте heartbeat.
    if _is_local_request(request):
        payload: dict = {
            "ok": ok,
            "heartbeat_fresh": fresh,
            "last_beat_age_seconds": _last_beat_age_seconds(),
        }
        if include_db:
            payload["db_ok"] = bool(db_ok)
    else:
        payload = {"ok": ok}

    return web.json_response(payload, status=200 if ok else 503)


async def _livez(request: web.Request) -> web.Response:
    """Liveness: процесс и event-loop живы. БД намеренно не проверяется."""
    return await _status_response(request, include_db=False)


async def _readyz(request: web.Request) -> web.Response:
    """Readiness: процесс жив и БД доступна."""
    return await _status_response(request, include_db=True)


async def _healthz(request: web.Request) -> web.Response:
    """Backward-compatible alias для старых ручных проверок.

    Сохраняем старую семантику ``/healthz`` как readiness, чтобы команда
    ``curl /healthz`` по-прежнему показывала и heartbeat, и DB status.
    Автоматический restart/rollback теперь должен смотреть на ``/livez``.
    """
    return await _readyz(request)


async def start(
    host: str = "0.0.0.0",  # nosec
    port: int = 8080,
) -> web.AppRunner:
    """Запустить health-сервер. Возвращает AppRunner для shutdown."""
    app = web.Application()
    app.router.add_get("/livez", _livez)
    app.router.add_get("/readyz", _readyz)
    app.router.add_get("/healthz", _healthz)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("healthcheck listening on %s:%s (/livez, /readyz, /healthz)", host, port)
    return runner


async def heartbeat_pulse(interval: float | None = None):
    """Фоновая задача: держит пульс свежим, пока жив asyncio-loop бота.

    Вызвать один раз на старте. Сосуществует с любым основным циклом
    бота. Её работа — только обновлять таймстемп из того же asyncio-цикла,
    которому принадлежит диспетчер, чтобы liveness оставался зелёным,
    пока этот цикл откликается.
    """
    if interval is None:
        interval = cfg.healthcheck_pulse_seconds
    while True:
        heartbeat.beat()
        await asyncio.sleep(interval)
