from maxapi import Dispatcher
from maxapi.filters.middleware import BaseMiddleware

from aemr_bot.handlers import (
    admin_commands,
    appeal,
    broadcast,
    menu,
    operator_reply,
    start,
)
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
    """Register handlers in command-first, catch-all-last order.

    `appeal.register` installs a `@dp.message_created()` without filters
    that is the catch-all citizen funnel router. maxapi dispatches handlers
    of the same update_type in registration order and stops at the first
    matching one — so the catch-all must come AFTER every Command-filtered
    handler, otherwise it silently swallows /stats, /reopen, /broadcast etc.
    before they reach their own decorators.

    `start.register` is fine to put first because all of its handlers ARE
    command-filtered. `menu.register` and `operator_reply.register` are
    no-ops kept for symmetry — actual callback / message routing for them
    lives in `appeal.on_callback` / `appeal.on_message`.
    """
    _attach_outer_middleware(dp, IdempotencyMiddleware())
    start.register(dp)
    admin_commands.register(dp)
    broadcast.register(dp)
    menu.register(dp)
    operator_reply.register(dp)
    # Catch-all last: see docstring above.
    appeal.register(dp)
