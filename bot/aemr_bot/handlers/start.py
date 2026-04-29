from maxapi import Dispatcher
from maxapi.types import BotStarted, Command, MessageCreated

from aemr_bot import keyboards, texts
from aemr_bot.db.session import session_scope
from aemr_bot.services import users as users_service


async def _ensure_user(event):
    user = getattr(event, "user", None) or getattr(event.message, "sender", None)
    max_user_id = getattr(user, "user_id", None) if user else None
    first_name = getattr(user, "first_name", None) if user else None
    if max_user_id is None:
        return None
    async with session_scope() as session:
        return await users_service.get_or_create(session, max_user_id=max_user_id, first_name=first_name)


async def cmd_start(event):
    await _ensure_user(event)
    await event.message.answer(texts.WELCOME, attachments=[keyboards.main_menu()])


async def cmd_help(event):
    await event.message.answer(texts.HELP_USER, attachments=[keyboards.main_menu()])


async def cmd_menu(event):
    await event.message.answer(texts.WELCOME, attachments=[keyboards.main_menu()])


async def cmd_forget(event):
    user = getattr(event, "user", None) or getattr(event.message, "sender", None)
    max_user_id = getattr(user, "user_id", None) if user else None
    if max_user_id is None:
        return
    async with session_scope() as session:
        from aemr_bot.services import operators as ops_service

        await users_service.erase_pdn(session, max_user_id)
        await ops_service.write_audit(
            session,
            operator_max_user_id=max_user_id,
            action="self_erase",
            target=f"user max_id={max_user_id}",
        )
    await event.message.answer(texts.ERASE_REQUESTED)


def register(dp: Dispatcher) -> None:
    @dp.bot_started()
    async def _(event: BotStarted):
        await cmd_start(event)

    @dp.message_created(Command("start"))
    async def _(event: MessageCreated):
        await cmd_start(event)

    @dp.message_created(Command("help"))
    async def _(event: MessageCreated):
        await cmd_help(event)

    @dp.message_created(Command("menu"))
    async def _(event: MessageCreated):
        await cmd_menu(event)

    @dp.message_created(Command("forget"))
    async def _(event: MessageCreated):
        await cmd_forget(event)
