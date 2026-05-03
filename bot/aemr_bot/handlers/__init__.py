from maxapi import Dispatcher
from maxapi.filters.middleware import BaseMiddleware

from aemr_bot.handlers import admin_commands, appeal, menu, operator_reply, start
from aemr_bot.services import idempotency


class IdempotencyMiddleware(BaseMiddleware):
    """Drop duplicate updates before they reach handlers."""

    async def __call__(self, handler, event_object, data):
        if not await idempotency.claim(event_object):
            return None
        return await handler(event_object, data)


def _attach_outer_middleware(dp: Dispatcher, middleware: BaseMiddleware) -> None:
    """Attach a middleware as outer across maxapi versions.

    The shape differs between releases: 0.9.18+ exposes a callable method
    `outer_middleware(mw)`; HEAD has a list `outer_middlewares`; older
    0.9.0–0.9.17 only had `middlewares`, where outer means insert-at-front.
    """
    add = getattr(dp, "outer_middleware", None)
    if callable(add):
        add(middleware)
        return
    bucket = getattr(dp, "outer_middlewares", None)
    if isinstance(bucket, list):
        bucket.append(middleware)
        return
    bucket = getattr(dp, "middlewares", None)
    if isinstance(bucket, list):
        bucket.insert(0, middleware)
        return
    raise RuntimeError(
        "maxapi.Dispatcher exposes no middleware hook — check installed version"
    )


def register_handlers(dp: Dispatcher) -> None:
    _attach_outer_middleware(dp, IdempotencyMiddleware())
    start.register(dp)
    menu.register(dp)
    appeal.register(dp)
    operator_reply.register(dp)
    admin_commands.register(dp)
