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


def register_handlers(dp: Dispatcher) -> None:
    dp.outer_middlewares.append(IdempotencyMiddleware())
    start.register(dp)
    menu.register(dp)
    appeal.register(dp)
    operator_reply.register(dp)
    admin_commands.register(dp)
