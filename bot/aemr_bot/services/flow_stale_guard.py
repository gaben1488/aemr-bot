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
    return _chars([1054,1073,1088,1072,1097,1077,1085,1080,1077,32,1091,1078,1077,32,1079,1072,1074,1077,1088,1096,1077,1085,1086,46,32,1045,1089,1083,1080,32,1089,1080,1090,1091,1072,1094,1080,1103,32,1072,1082,1090,1091,1072,1083,1100,1085,1072,44,32,1087,1086,1076,1072,1081,1090,1077,32,1087,1086,1093,1086,1078,1077,1077,32,1086,1073,1088,1072,1097,1077,1085,1080,1077,46])


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
