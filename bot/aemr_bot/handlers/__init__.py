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
    """Attach an outer middleware compatible with both maxapi attribute names.

    Newer maxapi (≥ post-2025-07) exposes a list `dp.outer_middlewares`.
    Older 0.6.0-ish revisions only had a singular `dp.outer_middleware`
    (sometimes a list, sometimes a single value). We probe in order and
    fall back to the public method if both attributes are missing.
    """
    bucket = getattr(dp, "outer_middlewares", None)
    if isinstance(bucket, list):
        bucket.append(middleware)
        return
    bucket = getattr(dp, "outer_middleware", None)
    if isinstance(bucket, list):
        bucket.append(middleware)
        return
    register = getattr(dp, "register_outer_middleware", None)
    if callable(register):
        register(middleware)
        return
    raise RuntimeError(
        "maxapi.Dispatcher does not expose an outer-middleware hook in any "
        "known shape — verify maxapi version (need a release that supports "
        "outer middlewares)."
    )


def register_handlers(dp: Dispatcher) -> None:
    _attach_outer_middleware(dp, IdempotencyMiddleware())
    start.register(dp)
    menu.register(dp)
    appeal.register(dp)
    operator_reply.register(dp)
    admin_commands.register(dp)
