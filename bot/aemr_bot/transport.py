"""MAX transport adapters: acknowledge only durable receipts, then dispatch locally."""
from __future__ import annotations

import asyncio
import hmac
import logging

from aiohttp import web
from maxapi import Dispatcher

from aemr_bot.services import inbox

log = logging.getLogger(__name__)


class DurableDispatcher(Dispatcher):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._inbox_task: asyncio.Task | None = None
        self._inbox_stop: asyncio.Event | None = None

    def _start_inbox(self, bot) -> None:
        if self._inbox_task is None:
            self._inbox_stop = asyncio.Event()
            self._inbox_task = asyncio.create_task(inbox.run(self, bot, self._inbox_stop), name="durable_inbox")

    async def startup(self, bot) -> None:
        await super().startup(bot)
        self._start_inbox(bot)

    async def _fetch_updates_once(self, bot):
        # SDK has completed its polling-specific subscription checks and startup.
        self._start_inbox(bot)
        return await super()._fetch_updates_once(bot)

    async def shutdown(self) -> None:
        if self._inbox_task is not None and self._inbox_stop is not None:
            await stop_worker(self._inbox_task, self._inbox_stop)
            self._inbox_task = None
        await super().shutdown()

    async def _dispatch_fetched_events(self, events: dict, current_timestamp: int, *, skip_updates: bool) -> None:
        try:
            updates = events.get("updates") or []
            if not isinstance(updates, list) or any(not isinstance(e, dict) for e in updates):
                raise ValueError("invalid batch")
            if skip_updates:
                updates = [e for e in updates if e.get("timestamp", current_timestamp) >= current_timestamp]
            await inbox.accept(updates)
            self._ensure_bot().marker_updates = events.get("marker")
        except Exception as exc:
            log.error("inbox receipt failed (%s); retaining polling marker", type(exc).__name__)
            await self._sleep_unless_stopped(2)


def setup_webhook(app: web.Application, *, secret: str) -> None:
    if not secret:
        raise ValueError("webhook secret required")

    async def receive(request: web.Request) -> web.Response:
        got = request.headers.get("X-Max-Bot-Api-Secret", "")
        if not hmac.compare_digest(got.encode(), secret.encode()):
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            payload = await request.json()
        except (ValueError, UnicodeError):
            return web.json_response({"error": "invalid json"}, status=400)
        if not isinstance(payload, dict) or not isinstance(payload.get("update_type"), str):
            return web.json_response({"error": "invalid event"}, status=400)
        try:
            await inbox.accept([payload])
        except Exception as exc:
            log.error("webhook receipt failed (%s)", type(exc).__name__)
            return web.json_response({"error": "temporarily unavailable"}, status=503)
        return web.json_response({"ok": True})

    app.router.add_post("/max/webhook", receive)


async def stop_worker(task: asyncio.Task, stop: asyncio.Event) -> None:
    stop.set()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=10)
    except TimeoutError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
