"""Маленький aiohttp-сервер /healthz. Работает всегда, независимо от режима бота.

Использует один глобальный экземпляр Heartbeat, обновляемый из циклов
polling/диспетчера. /healthz возвращает 200, если пульс свежий (моложе
``HEALTHCHECK_STALE_SECONDS`` секунд) И база отвечает на ping, иначе 503.

В режиме self-host с long-polling у бота нет публично доступного входящего
порта: /healthz слушает на 127.0.0.1 и используется блоком healthcheck в
docker-compose плюс внутренней cron-задачей ``selfcheck``. Внешние
пингеры (UptimeRobot, Healthchecks.io и т. п.) не задействованы; внутренний
сборщик здоровья из сети заказчика может подключиться через исходящий
импульс на HEALTHCHECK_URL.
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
    данные, виснет. Без этого ping'а /healthz отдавал бы половину правды:
    OK, при этом жители не видят ответов.
    """
    from sqlalchemy import text

    from aemr_bot.db.session import session_scope

    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        log.warning("healthz: DB ping failed", exc_info=True)
        return False


# Кэшируем результат ping'а БД на короткий интервал, чтобы плотная серия
# проверок (healthcheck в compose каждые 30 секунд плюс любой внутренний
# пингер, который подключит админ) не превращалась в шквал тривиальных
# SELECT'ов, конкурирующих с настоящими обработчиками за маленький пул
# соединений.
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


async def _healthz(request: web.Request) -> web.Response:
    fresh = heartbeat.is_fresh()
    db_ok = await _ping_db_cached()
    ok = fresh and db_ok
    payload = {
        "ok": ok,
        "heartbeat_fresh": fresh,
        "db_ok": db_ok,
        "last_beat_age_seconds": (
            None if heartbeat.last_beat == 0.0 else round(time.monotonic() - heartbeat.last_beat, 1)
        ),
    }
    return web.json_response(payload, status=200 if ok else 503)


async def start(host: str = "0.0.0.0", port: int = 8080) -> web.AppRunner:  # nosec B104 — слушаем внутри контейнера, наружу выставляет Nginx
    """Запустить /healthz на (host, port). Возвращает AppRunner, чтобы вызывающий мог его остановить."""
    app = web.Application()
    app.router.add_get("/healthz", _healthz)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("healthcheck listening on %s:%s/healthz", host, port)
    return runner


async def heartbeat_pulse(interval: float | None = None):
    """Фоновая задача: держит пульс свежим, пока жив polling-цикл бота.

    Вызвать один раз на старте. Сосуществует с любым основным циклом
    бота. Её работа — только обновлять таймстемп из того же asyncio-цикла,
    которому принадлежит диспетчер, чтобы /healthz оставался зелёным,
    пока этот цикл откликается.
    """
    if interval is None:
        interval = cfg.healthcheck_pulse_seconds
    while True:
        heartbeat.beat()
        await asyncio.sleep(interval)
