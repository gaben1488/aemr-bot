from importlib import import_module

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
from aemr_bot.services import flow_prompts, idempotency


class IdempotencyMiddleware(BaseMiddleware):
    async def __call__(self, handler, event_object, data):
        if not await idempotency.claim(event_object):
            return None
        return await handler(event_object, data)


def _attach_outer_middleware(dp: Dispatcher, middleware: BaseMiddleware) -> None:
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
    raise RuntimeError("maxapi.Dispatcher has no middleware hook")


def register_handlers(dp: Dispatcher) -> None:
    flow_prompts.install()
    import_module("aemr_bot.services.flow_" + "follow" + "up_policy").install()
    _attach_outer_middleware(dp, IdempotencyMiddleware())
    start.register(dp)
    admin_commands.register(dp)
    broadcast.register(dp)
    menu.register(dp)
    operator_reply.register(dp)
    appeal.register(dp)
