from maxapi import Dispatcher

from aemr_bot.handlers import admin_commands, appeal, menu, operator_reply, start


def register_handlers(dp: Dispatcher) -> None:
    start.register(dp)
    menu.register(dp)
    appeal.register(dp)
    operator_reply.register(dp)
    admin_commands.register(dp)
