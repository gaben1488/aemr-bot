"""Tiny aiohttp /healthz server. Always running, regardless of bot mode.

Uses a single global Heartbeat instance updated from the polling/dispatcher
loops. /healthz returns 200 if the heartbeat is fresh (less than
``HEALTHCHECK_STALE_SECONDS`` ago) AND the DB pings, 503 otherwise.

In self-host long-polling mode the bot has no public inbound port —
/healthz is bound to 127.0.0.1 and consumed by the compose healthcheck
block plus the internal ``selfcheck`` cron job. No external pinger
(UptimeRobot/Healthchecks.io etc.) is in the loop; an internal
health-collector inside the customer's network can opt in via the
outbound HEALTHCHECK_URL pulse.
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
    """SELECT 1 against the live engine. Returns False on any failure.

    A bot with a frozen DB connection can keep its asyncio event-loop
    spinning (and the heartbeat green) while every operation that
    actually touches data hangs. Without this ping, /healthz is a
    half-truth — it would return OK while citizens see no responses.
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


# Cache the DB-ping result for a short window so that a busy chain of
# probes (compose healthcheck every 30s plus any internal pinger the
# admin wires up) doesn't multiply into a flurry of trivial SELECTs
# that compete with real handlers for the small connection pool.
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


async def start(host: str = "0.0.0.0", port: int = 8080) -> web.AppRunner:  # nosec B104 — bind inside container, expose via Nginx
    """Start /healthz on (host, port). Returns AppRunner so the caller can stop it."""
    app = web.Application()
    app.router.add_get("/healthz", _healthz)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("healthcheck listening on %s:%s/healthz", host, port)
    return runner


async def heartbeat_pulse(interval: float | None = None):
    """Background task: keep the heartbeat fresh while the bot's polling loop is alive.

    Call this once at startup. It coexists with whatever main loop the bot
    uses — its job is solely to update the timestamp from the asyncio loop
    that owns the dispatcher, so /healthz stays green as long as that loop
    is responsive.
    """
    if interval is None:
        interval = cfg.healthcheck_pulse_seconds
    while True:
        heartbeat.beat()
        await asyncio.sleep(interval)
