from __future__ import annotations

import logging

from sqlalchemy import desc, select

from aemr_bot.db.models import Appeal

log = logging.getLogger(__name__)

OPEN_STATUSES = {"new", "in_progress"}
DONE_STATUSES = {"answered", "closed"}


def _chars(values: list[int]) -> str:
    return "".join(chr(v) for v in values)


def _add_label() -> str:
    return chr(128206) + " " + _chars([1044, 1086, 1087, 1086, 1083, 1085, 1080, 1090, 1100])


def _repeat_label() -> str:
    return chr(128257) + " " + _chars([1055, 1086, 1076, 1072, 1090, 1100, 32, 1087, 1086, 1093, 1086, 1078, 1077, 1077])


def _menu_label() -> str:
    return chr(8617) + chr(65039) + " " + _chars([1042, 32, 1084, 1077, 1085, 1102])


def can_append(status: str | None) -> bool:
    return status in OPEN_STATUSES


async def find_open_for_user(session, user_id: int) -> Appeal | None:
    return await session.scalar(
        select(Appeal)
        .where(Appeal.user_id == user_id, Appeal.status.in_(list(OPEN_STATUSES)))
        .order_by(desc(Appeal.created_at))
        .limit(1)
    )


def user_card_keyboard(appeal_id: int, status: str):
    from maxapi.types import CallbackButton
    from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

    kb = InlineKeyboardBuilder()
    if status in OPEN_STATUSES:
        kb.row(CallbackButton(text=_add_label(), payload=f"appeal:followup:{appeal_id}"))
    elif status in DONE_STATUSES:
        kb.row(CallbackButton(text=_repeat_label(), payload=f"appeal:repeat:{appeal_id}"))
    kb.row(CallbackButton(text=_menu_label(), payload="menu:main"))
    return kb.as_markup()


def install() -> None:
    from aemr_bot import keyboards
    from aemr_bot.services import appeals as appeals_service

    keyboards.user_appeal_card_keyboard = user_card_keyboard
    appeals_service.find_active_for_user = find_open_for_user
    log.info("followup visibility policy installed")
