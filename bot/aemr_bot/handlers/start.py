import logging

from maxapi import Dispatcher
from maxapi.types import BotStarted, Command, MessageCreated

from aemr_bot import keyboards, texts
from aemr_bot.db.session import session_scope
from aemr_bot.services import operators as ops_service
from aemr_bot.services import policy as policy_service
from aemr_bot.services import settings_store
from aemr_bot.services import users as users_service
from aemr_bot.utils.event import get_chat_id, get_first_name, get_user_id, reply

log = logging.getLogger(__name__)


async def _ensure_user(event):
    max_user_id = get_user_id(event)
    first_name = get_first_name(event)
    if max_user_id is None:
        return None
    async with session_scope() as session:
        return await users_service.get_or_create(session, max_user_id=max_user_id, first_name=first_name)


async def cmd_start(event):
    await _ensure_user(event)
    await reply(event, texts.WELCOME, attachments=[keyboards.main_menu()])


async def cmd_help(event):
    await reply(event, texts.HELP_USER, attachments=[keyboards.main_menu()])


async def cmd_menu(event):
    await reply(event, texts.WELCOME, attachments=[keyboards.main_menu()])


async def cmd_policy(event):
    """Send the privacy policy PDF to the citizen on demand."""
    chat_id = get_chat_id(event)
    if chat_id is None:
        return

    async with session_scope() as session:
        token = await settings_store.get(session, policy_service.POLICY_TOKEN_KEY)
        policy_url = await settings_store.get(session, "policy_url")

    bot = getattr(event, "bot", None)

    # Cold start safety: try to upload the PDF if the token hasn't been cached
    # yet (e.g. first runs after deploy where startup upload silently failed).
    if not token and bot is not None:
        try:
            token = await policy_service.ensure_uploaded(bot)
        except Exception:
            log.exception("on-demand policy upload failed")

    if token and bot is not None:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=texts.POLICY_DELIVERED,
                attachments=[policy_service.build_file_attachment(token)],
            )
            return
        except Exception:
            log.exception("policy file delivery failed; falling back to URL")

    if policy_url:
        await reply(event, texts.POLICY_FALLBACK_URL.format(policy_url=policy_url))
    else:
        await reply(event, texts.POLICY_UNAVAILABLE)


async def cmd_forget(event):
    max_user_id = get_user_id(event)
    if max_user_id is None:
        return
    async with session_scope() as session:
        await users_service.erase_pdn(session, max_user_id)
        await ops_service.write_audit(
            session,
            operator_max_user_id=max_user_id,
            action="self_erase",
            target=f"user max_id={max_user_id}",
        )
    await reply(event, texts.ERASE_REQUESTED)


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

    @dp.message_created(Command("policy"))
    async def _(event: MessageCreated):
        await cmd_policy(event)

    @dp.message_created(Command("whoami"))
    async def _(event: MessageCreated):
        max_user_id = get_user_id(event) or "?"
        first_name = get_first_name(event) or ""
        chat_id = get_chat_id(event) or "?"
        await reply(
            event,
            "🛠 whoami\n"
            f"max_user_id: {max_user_id}\n"
            f"first_name: {first_name}\n"
            f"chat_id: {chat_id}",
        )
