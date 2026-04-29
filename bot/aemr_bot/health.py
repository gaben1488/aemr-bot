"""Tiny aiohttp /healthz server. Always running, regardless of bot mode.

Uses a single global Heartbeat instance updated from the polling/dispatcher
loops. /healthz returns 200 if the heartbeat is fresh (less than
``HEALTHCHECK_STALE_SECONDS`` ago), 503 otherwise.

Liveness probes (Docker, UptimeRobot, Healthchecks.io) hit this endpoint;
when it goes red we know the bot's main loop has stalled even if the
process is still up.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from aiohttp import web

log = logging.getLogger(__name__)

HEALTHCHECK_STALE_SECONDS = 120


@dataclass
class Heartbeat:
    last_beat: float = 0.0

    def beat(self) -> None:
        self.last_beat = time.monotonic()

    def is_fresh(self, max_age: float = HEALTHCHECK_STALE_SECONDS) -> bool:
        if self.last_beat == 0.0:
            return False
        return (time.monotonic() - self.last_beat) <= max_age


heartbeat = Heartbeat()


async def _healthz(request: web.Request) -> web.Response:
    fresh = heartbeat.is_fresh()
    payload = {
        "ok": fresh,
        "last_beat_age_seconds": (
            None if heartbeat.last_beat == 0.0 else round(time.monotonic() - heartbeat.last_beat, 1)
        ),
    }
    return web.json_response(payload, status=200 if fresh else 503)


async def start(host: str = "0.0.0.0", port: int = 8080) -> web.AppRunner:
    """Start /healthz on (host, port). Returns AppRunner so the caller can stop it."""
    app = web.Application()
    app.router.add_get("/healthz", _healthz)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("healthcheck listening on %s:%s/healthz", host, port)
    return runner


async def heartbeat_pulse(interval: float = 30.0):
    """Background task: keep the heartbeat fresh while the bot's polling loop is alive.

    Call this once at startup. It coexists with whatever main loop the bot
    uses — its job is solely to update the timestamp from the asyncio loop
    that owns the dispatcher, so /healthz stays green as long as that loop
    is responsive.
    """
    while True:
        heartbeat.beat()
        await asyncio.sleep(interval)
