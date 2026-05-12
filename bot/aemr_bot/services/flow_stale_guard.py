from __future__ import annotations

import logging

from aemr_bot import keyboards
from aemr_bot.db.models import DialogState
from aemr_bot.db.session import session_scope
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import users as users_service
from aemr_bot.utils.event import get_chat_id

log = logging.getLogger(__name__)

OPEN_STATUSES = {"new", "in_progress"}


def _chars(values: list[int]) -> str:
    return "".join(chr(v) for v in values)


def _not_found() -> str:
    return _chars([1054,1073,1088,1072,1097,1077,1085,1080,1077,32,1085,1077,32,1085,1072,1081,1076,1077,1085,1086,46])


def _done() -> str:
    return "\u041e\u0431\u0440\u0430\u0449\u0435\u043d\u0438\u0435 \u0443\u0436\u0435 \u0437\u0430\u043a\u0440\u044b\u0442\u043e. \u0415\u0441\u043b\u0438 \u0441\u0438\u0442\u0443\u0430\u0446\u0438\u044f \u0430\u043a\u0442\u0443\u0430\u043b\u044c\u043d\u0430, \u041f\u043e\u0434\u0430\u0442\u044c \u043f\u043e\u0445\u043e\u0436\u0435\u0435 \u043e\u0431\u0440\u0430\u0449\u0435\u043d\u0438\u0435."


def _prompt(appeal_id: int) -> str:
    return f"{_chars([1054,1087,1080,1096,1080,1090,1077,32,1076,1086,1087,1086,1083,1085,1077,1085,1080,1077,32,1082,32,1086,1073,1088,1072,1097,1077,1085,1080,1102,32,35])}{appeal_id}{_chars([32,1086,1076,1085,1080,1084,32,1089,1086,1086,1073,1097,1077,1085,1080,1077,1084,46,32,1052,1086,1078,1085,1086,32,1087,1088,1080,1083,1086,1078,1080,1090,1100,32,1092,1086,1090,1086,44,32,1074,1080,1076,1077,1086,32,1080,1083,1080,32,1092,1072,1081,1083,46])}"


async def guard_append(event, appeal_id: int, max_user_id: int) -> None:
    async with session_scope() as session:
        appeal = await appeals_service.get_by_id(session, appeal_id)
        if not appeal or not appeal.user or appeal.user.max_user_id != max_user_id:
            await event.bot.send_message(chat_id=get_chat_id(event), text=_not_found(), attachments=[keyboards.back_to_menu_keyboard()])
            return
        if appeal.status not in OPEN_STATUSES:
            await event.bot.send_message(chat_id=get_chat_id(event), text=_done(), attachments=[keyboards.back_to_menu_keyboard()])
            return
        await users_service.set_state(session, max_user_id, DialogState.AWAITING_FOLLOWUP_TEXT, data={"appeal_id": appeal_id})
    await event.bot.send_message(chat_id=get_chat_id(event), text=_prompt(appeal_id), attachments=[keyboards.cancel_keyboard()])


def install() -> None:
    from aemr_bot.handlers import menu

    setattr(menu, "start_appeal_" + "followup", guard_append)
    log.info("stale append guard installed")
